#!/usr/bin/env python3
"""The plugin seam, and what it refuses.

Adding an analytics source used to mean editing six core files — the step table,
the merge, the emitter, the validator, the findings builder and the dashboard.
The reason was a category error rather than a missing abstraction: a traffic
figure was being treated as a REGISTRY FACT. It is not one. The registry answers
what EXISTS, is validated, lives in git and is rewritten whole on every emit; a
measurement taken at an instant accumulates and belongs beside `events`.

So the seam is one contract, and the interesting half is not that a plugin can
write — it is what happens when one misbehaves. A plugin that can write any
metric name is a plugin whose output nobody can check, and every rule below is
driven with a plugin planted to break it.
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup, PROJECT_ID as SYNTHETIC_PROJECT
portable_setup()

sys.path.insert(0, str(ROOT / "tests"))
import live_estate                                                  # noqa: E402
import tmp as tmpdir  # noqa: E402
import paths                                                        # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def sandbox(manifest: dict, script: str) -> tuple[pathlib.Path, pathlib.Path]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plugin-"))
    plugins = d / "plugins"
    plugins.mkdir()
    (plugins / f"{manifest['id']}.json").write_text(json.dumps(manifest), encoding="utf-8")
    if manifest.get("script"):
        (plugins / manifest["script"]).write_text(script, encoding="utf-8")
    return plugins, d / "store.db"


def run(plugins: pathlib.Path, db: pathlib.Path, *args: str) -> str:
    p = subprocess.run([PY, "collectors/run_plugins.py", "--force", *args], cwd=ROOT,
                       capture_output=True, text=True, timeout=300,
                       # The scratch dir too: the runner writes its report to
                       # `plugins.json` there, and the live one is what
                       # `build_findings` reads.
                       env={**os.environ, "OBSERVATORY_PLUGINS": str(plugins),
                            "OBSERVATORY_DB": str(db),
                            "OBSERVATORY_SCRATCH": str(db.parent)})
    return p.stdout + p.stderr


def real_project() -> str:
    return json.loads((paths.REGISTRY / "projects.json")
                      .read_text(encoding="utf-8"))["projects"][0]["id"]


def emitter(rows: list[dict]) -> str:
    return "import json\n" + "\n".join(f"print(json.dumps({r!r}))" for r in rows)


BASE = {"id": "probe", "title": "t", "script": "probe.py", "every_hours": 0,
        "requires": [], "why": "planted by the test",
        "metrics": [{"name": "test.value", "unit": "n", "means": "a number"}]}


def test_a_well_behaved_plugin_is_recorded() -> None:
    pid = real_project()
    plugins, db = sandbox(BASE, emitter([
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07T00:00:00Z", "value": 42}]))
    out = run(plugins, db)
    check("it reports what it wrote", "probe: 1 measurement" in out, out[-200:])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute("SELECT value, unit, source FROM metrics").fetchone()
    check("the value lands", row and row[0] == 42.0, str(row))
    check("the unit comes from the manifest, not the plugin", row[1] == "n", str(row))
    check("the source is the plugin id, so a row can be traced back", row[2] == "probe", str(row))


def test_re_measuring_replaces_rather_than_duplicates() -> None:
    pid = real_project()
    plugins, db = sandbox(BASE, emitter([
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07T00:00:00Z", "value": 1}]))
    run(plugins, db)
    (plugins / "probe.py").write_text(emitter([
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07T00:00:00Z", "value": 2}]),
        encoding="utf-8")
    run(plugins, db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT value FROM metrics").fetchall()
    check("one row, not two", len(rows) == 1, str(rows))
    check("and it holds the newer value", rows and rows[0][0] == 2.0, str(rows))


def test_an_undeclared_metric_is_refused() -> None:
    """A plugin that can write any name is one whose output nobody can check."""
    pid = real_project()
    plugins, db = sandbox(BASE, emitter([
        {"project_id": pid, "metric": "something.i.invented", "at": "2026-09-07T00:00:00Z",
         "value": 1}]))
    out = run(plugins, db)
    check("the row is refused", "REFUSED" in out and "undeclared metric" in out, out[-200:])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    check("and nothing is written", conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0)


def test_an_unknown_project_and_a_bad_timestamp_are_refused() -> None:
    pid = real_project()
    plugins, db = sandbox(BASE, emitter([
        {"project_id": "project:does-not-exist", "metric": "test.value",
         "at": "2026-09-07T00:00:00Z", "value": 1},
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07 00:00:00+02:00", "value": 1},
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07T00:00:00Z", "value": "abc"},
    ]))
    out = run(plugins, db)
    check("an unknown project is refused", "unknown project" in out, out[-300:])
    check("a timestamp that is not UTC Z is refused", "not UTC Z" in out, out[-300:])
    check("a non-numeric value is refused", "not a number" in out, out[-300:])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    check("nothing survives", conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0)


def test_a_missing_requirement_skips_with_a_reason() -> None:
    """The rule every collector here follows: not read is a state, not a failure."""
    m = dict(BASE, requires=["env:OBSERVATORY_NO_SUCH_VARIABLE"])
    plugins, db = sandbox(m, emitter([]))
    out = run(plugins, db)
    check("it skips", "SKIP" in out, out[-200:])
    check("and names what is missing",
          "OBSERVATORY_NO_SUCH_VARIABLE is not set" in out, out[-200:])


def test_a_crashing_plugin_does_not_stop_the_others() -> None:
    pid = real_project()
    plugins, db = sandbox(BASE, "raise SystemExit('deliberate')")
    good = dict(BASE, id="good", script="good.py")
    (plugins / "good.json").write_text(json.dumps(good), encoding="utf-8")
    (plugins / "good.py").write_text(emitter([
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07T00:00:00Z", "value": 7}]),
        encoding="utf-8")
    out = run(plugins, db)
    check("the crash is reported", "SKIP  probe" in out and "exited" in out, out[-300:])
    check("and the other plugin still ran", "good: 1 measurement" in out, out[-300:])


def test_a_manifest_declaring_no_metrics_is_refused_outright() -> None:
    m = dict(BASE, metrics=[])
    plugins, db = sandbox(m, emitter([]))
    out = run(plugins, db)
    check("it skips with the reason", "metrics must be a non-empty list" in out, out[-200:])


def run_timed(plugins: pathlib.Path, db: pathlib.Path, seconds: int) -> str:
    p = subprocess.run([PY, "collectors/run_plugins.py", "--force"], cwd=ROOT,
                       capture_output=True, text=True, timeout=300,
                       env={**os.environ, "OBSERVATORY_PLUGINS": str(plugins),
                            "OBSERVATORY_DB": str(db), "OBSERVATORY_SCRATCH": str(db.parent),
                            "OBSERVATORY_PLUGIN_TIMEOUT_SECONDS": str(seconds)})
    return p.stdout + p.stderr


def plugin_report(db: pathlib.Path) -> dict:
    doc = json.loads((db.parent / "plugins.json").read_text(encoding="utf-8"))
    return next(p for p in doc["plugins"] if p["id"] == "probe")


def test_a_plugin_stopped_at_its_limit_keeps_what_it_measured() -> None:
    """2026-10-09/10: `disk-usage` walked ~100 GB, reached the runner's 300 s limit and
    lost every row it had printed — so no sample existed for the period, the plugin was
    due again on the next tick, and spent 300 s of the tail every half hour."""
    pid = real_project()
    plugins, db = sandbox(BASE, emitter([
        {"project_id": pid, "metric": "test.value", "at": "2026-09-07T00:00:00Z", "value": 7}])
        + "\nimport sys, time\nsys.stdout.flush()\ntime.sleep(60)\n")
    out = run_timed(plugins, db, 2)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute("SELECT value FROM metrics").fetchall()
    check("the row printed before the limit is kept", rows == [(7.0,)], f"{rows} {out[-200:]}")
    r = plugin_report(db)
    check("and the run is partial, not broken", r.get("classification") != "broken" and r.get("partial"),
          json.dumps(r)[:300])
    check("saying so where a person reads it", "partial" in out, out[-200:])


def test_a_plugin_that_printed_nothing_by_its_limit_is_still_broken() -> None:
    plugins, db = sandbox(BASE, "import time\ntime.sleep(60)\n")
    run_timed(plugins, db, 2)
    r = plugin_report(db)
    check("nothing measured is a broken run", r.get("classification") == "broken", json.dumps(r)[:300])


def test_a_plugin_is_told_its_deadline() -> None:
    """So it can stop between units of work instead of being killed in the middle of one."""
    pid = real_project()
    script = ("import json, os, time\n"
              "left = float(os.environ['OBSERVATORY_STEP_DEADLINE']) - time.time()\n"
              f"print(json.dumps({{'project_id': {pid!r}, 'metric': 'test.value', "
              "'at': '2026-09-07T00:00:00Z', 'value': left}))\n")
    plugins, db = sandbox(BASE, script)
    run_timed(plugins, db, 20)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute("SELECT value FROM metrics").fetchone()
    check("the deadline it is given falls inside its own limit", row is not None and 0 < row[0] <= 20,
          str(row))


def disk_usage_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("disk_usage_under_test", ROOT / "plugins/disk_usage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_disk_usage_walks_each_folder_once() -> None:
    """The footprint and the reclaimable bytes came from two full walks of every tree; one
    walk answers both."""
    du = disk_usage_module()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-du-"))
    (d / "src").mkdir()
    (d / "src/a.txt").write_bytes(b"x" * 1000)
    (d / "node_modules/pkg").mkdir(parents=True)
    (d / "node_modules/pkg/i.js").write_bytes(b"y" * 5000)
    (d / ".git").mkdir()
    (d / ".git/HEAD").write_bytes(b"z" * 300)
    footprint, reclaimable = du.measure(d)
    check("the working files are the footprint", footprint == 1000, str(footprint))
    check("the reinstallable directories are the reclaimable part", reclaimable == 5300, str(reclaimable))


def test_disk_usage_measures_the_longest_unmeasured_first() -> None:
    du = disk_usage_module()
    last = {"a": "2026-10-09T00:00:00Z", "b": "2026-10-01T00:00:00Z"}
    order = du.oldest_first([{"id": "a"}, {"id": "b"}, {"id": "c"}], last)
    check("never measured, then the oldest measurement", [p["id"] for p in order] == ["c", "b", "a"],
          str([p["id"] for p in order]))


def test_disk_usage_stops_between_projects_at_its_deadline() -> None:
    pid = real_project()
    env = {**os.environ, "OBSERVATORY_STEP_DEADLINE": "1"}   # long past
    p = subprocess.run([PY, "plugins/disk_usage.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    check("it exits cleanly", p.returncode == 0, p.stderr[-200:])
    check("having measured nothing it had no time for", p.stdout.strip() == "", p.stdout[:200])
    check("and it says the run was partial", "partial" in p.stderr, p.stderr[-200:])
    del pid


def test_the_seam_needs_no_core_file() -> None:
    """The claim the whole iteration rests on, stated as a check."""
    # ONE READER, in one place, and it is the right one for THIS rule.
    # `tests/source_reader.py` documents why neither of the other two fits: one
    # blanks string literals and would miss `WHERE metric = 'disk.bytes'` — the
    # exact defect `tools/build_findings.py` shipped on 2026-09-07 — and the
    # other keeps them and so flags a `//` comment inside the dashboard's
    # embedded JavaScript.
    import source_reader
    code_only = source_reader.code_keeping_strings

    for core in ("collectors/merge.py", "collectors/emit_registry.py",
                 "tools/validate_registry.py", "tools/build_findings.py",
                 "dashboard/build_dashboard.py"):
        src = code_only((ROOT / core).read_text(encoding="utf-8"))
        check(f"{core} does not name a plugin in code",
              "disk-usage" not in src and "disk.bytes" not in src,
              "a core file naming a plugin is the six-file problem returning")
    manifest = json.loads((ROOT / "plugins/disk-usage.json").read_text(encoding="utf-8"))
    # THE RULE, not a literal list. This demanded exactly `["disk.bytes"]`, so
    # the plugin gaining the two metrics that answer "what do its extra
    # checkouts cost" and "what would free the disk" failed a test about
    # DECLARING metrics. What must hold is that every metric the
    # runner will accept is declared with a meaning — the reference plugin
    # having one metric was never the point.
    names = [x["name"] for x in manifest["metrics"]]
    check("the shipped plugin declares what it produces",
          names and all(n.startswith("disk.") for n in names), str(names))
    check("and each declaration says what the number means",
          all(len(x.get("means", "")) > 60 for x in manifest["metrics"]),
          str([x["name"] for x in manifest["metrics"] if len(x.get("means", "")) <= 60]))
    check("with no two declaring the same name", len(set(names)) == len(names), str(names))
    check("and says what question it answers", len(manifest.get("why", "")) > 40)


def test_the_reference_plugin_measured_the_live_estate() -> None:
    if not paths.DB.exists():
        print("  SKIP  no store on this machine")
        return
    conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    n, total = conn.execute(
        "SELECT COUNT(*), SUM(value) FROM metrics WHERE metric='disk.bytes'").fetchone()
    # THE ROWS, not the file. A store that does not exist is CREATED by the first
    # read-write connect — sqlite makes the file and the migrations fill it — so
    # "is there a store" is nearly always true and answers the wrong question.
    # What an assertion needs is the rows it asserts about.
    if not live_estate.needs("plugin measurements in the store", (n or 0) > 0,
                             "the plugin contract and its degradations are driven "
                             "against fixtures earlier in this suite"):
        return
    check("it produced a measurement per project with folders", (n or 0) == 16, str(n))
    check("and the total is a plausible estate size", (total or 0) > 1e9, str(total))



def test_a_plugin_written_from_the_readme_alone_runs() -> None:
    """The document's central promise, driven.

    `plugins/README.md` says adding a plugin touches no file outside its
    directory. Tested on 2026-09-08 by authoring one from the document alone —
    manifest, script, no reading of the runner — and it ran: 91 rows, none
    refused. What the README did NOT say was how a script finds anything: it
    hands the script no arguments, no stdin and no project list, and the
    resolvers the repository has for the estate root and the registry are never
    mentioned. So the plugin inlined `Path.home() / "DATA"` and
    `Path("registry/projects.json")` — right on one machine, wrong on every
    other — and `tools/check_paths.py` had a rule for neither spelling
               

    This drives the shape the README now teaches: import `paths` from the root,
    read the registry through it, print one row per line.
    """
    pid = real_project()
    script = (
        "import json, pathlib, sys\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        # The plugin directory is redirected here, so `parents[1]` is the temp
        # sandbox and `paths` has to come from the repository the runner starts
        # in. A real plugin lives under the checkout, where `parents[1]` IS the
        # root — the import line the README teaches is the one asserted below.
        "sys.path.insert(0, %r)\n"
        "import paths\n"
        "projects = json.loads((paths.REGISTRY / 'projects.json')"
        ".read_text(encoding='utf-8'))['projects']\n"
        "print(json.dumps({'project_id': %r, 'metric': 'readme.rows',\n"
        "                  'at': '2026-09-08T00:00:00Z',\n"
        "                  'value': float(len(projects))}))\n"
    ) % (str(ROOT), pid)
    plugins, db = sandbox({"id": "readme-example", "title": "From the document",
                           "script": "readme_example.py",
                           "metrics": [{"name": "readme.rows", "unit": "rows",
                                        "means": "how many projects the registry "
                                                 "held when this ran"}],
                           "every_hours": 24, "requires": [],
                           "why": "the README's own shape, driven"}, script)
    out = run(plugins, db)
    check("the runner accepts a plugin written to the document",
          "readme-example" in out and "broken" not in out, out[-300:])
    rows = json.loads((db.parent / "plugins.json").read_text(encoding="utf-8"))
    mine = [e for e in rows.get("plugins", []) if e.get("id") == "readme-example"]
    check("it wrote its row", mine and mine[0].get("written") == 1, str(mine)[:200])
    check("and refused nothing", mine and not mine[0].get("refused"), str(mine)[:200])


def test_the_readme_tells_an_author_what_the_script_is_given() -> None:
    """The silence that produced the two inlined paths. Asserted on the document
    because the document is the artefact an author has."""
    text = (ROOT / "plugins/README.md").read_text(encoding="utf-8")
    check("it says nothing is handed to the script",
          "No arguments, no stdin" in text,
          "an author who expects the project list on stdin writes a script that "
          "reads stdin, gets nothing, and has nothing to debug with")
    check("it names the resolvers rather than leaving them to be guessed",
          "paths.DATA" in text and "paths.REGISTRY" in text, "")
    check("and says what happens to a plugin that inlines them instead",
          "paths-current" in text, "")


if __name__ == "__main__":
    print("plugins — one contract, and what it refuses\n")
    for fn in (test_a_well_behaved_plugin_is_recorded,
               test_re_measuring_replaces_rather_than_duplicates,
               test_an_undeclared_metric_is_refused,
               test_an_unknown_project_and_a_bad_timestamp_are_refused,
               test_a_missing_requirement_skips_with_a_reason,
               test_a_crashing_plugin_does_not_stop_the_others,
               test_a_manifest_declaring_no_metrics_is_refused_outright,
               test_a_plugin_stopped_at_its_limit_keeps_what_it_measured,
               test_a_plugin_that_printed_nothing_by_its_limit_is_still_broken,
               test_a_plugin_is_told_its_deadline,
               test_disk_usage_walks_each_folder_once,
               test_disk_usage_measures_the_longest_unmeasured_first,
               test_disk_usage_stops_between_projects_at_its_deadline,
               test_the_seam_needs_no_core_file,
               test_the_reference_plugin_measured_the_live_estate,
               test_a_plugin_written_from_the_readme_alone_runs,
               test_the_readme_tells_an_author_what_the_script_is_given):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma new analytics source is two files in plugins/\033[0m")
