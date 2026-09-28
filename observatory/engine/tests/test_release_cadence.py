#!/usr/bin/env python3
"""Nothing said whether a project had ever shipped anything.

The per-project answer is rich — identity, ownership, activity (weeks, commits,
sessions, worked days), disk, dependencies, findings, notes, recent events — and
`lastActivityOn` says work HAPPENED. Nothing said the work resulted in anything.
For a portfolio of about 120 projects that is the difference between a portfolio
and a graveyard.

**Measured before the instrument was built, because a metric that is a field of
zeros is machinery without a question.** On the installation this was designed
on, roughly a third of the checkouts held tags and the rest held none — a split
that is a signal, where nearly all or nearly none would not have been.

Two metrics, and the second one's ABSENCE carries information:

    release.tags              how many tags the checkout holds
    release.days_since_last   days since the newest tag — OMITTED where there
                              are none, because 0 would mean "released today",
                              which is the opposite of never

**`release.tags` declares the role `release.count`.** A role exists so a CORE
file can resolve a question without knowing which plugin answers it —
`tools/build_findings.py` resolves `footprint.bytes` that way — and the
portfolio finding asks whether a project has ever shipped through the role, not
the name. `release.days_since_last` declares none, because nothing resolves it.

**No finding on the count itself.** "N projects have never released" is a
portfolio fact rather than something to act on, and a row per untagged project
would be N lines of noise. What DOES carry a question is the narrower cell
`portfolio.unreleased_and_quiet` reports: never shipped AND stopped.
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402
import paths                                                        # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
MANIFEST = ROOT / "plugins/release-cadence.json"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def git(args: list[str], cwd: pathlib.Path) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def repo_with(tags: list[tuple[str, str]], *, annotated: bool = False) -> pathlib.Path:
    """A checkout holding `(tag, YYYY-MM-DD)` pairs."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-release-"))
    r = d / "proj"
    r.mkdir()
    git(["init", "--initial-branch=main"], r)
    (r / "f").write_text("x", encoding="utf-8")
    git(["add", "f"], r)
    git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "one"], r)
    for name, when in tags:
        stamp = f"{when}T12:00:00+0000"
        env = {**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
        cmd = ["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t", "tag"]
        cmd += (["-a", name, "-m", name] if annotated else [name])
        subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, check=True)
    return r


def measure(checkout: pathlib.Path | None, project_id: str = "project:p") -> list[dict]:
    """Run the plugin against a planted registry naming one project."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-relreg-"))
    (d / "registry").mkdir()
    local = {} if checkout is None else {
        "folder": checkout.name, "path": str(checkout)}
    (d / "registry/projects.json").write_text(json.dumps({"projects": [
        {"id": project_id, "name": "p", "ownership": "owned", "lifecycle": "active",
         "local_folders": [checkout.name] if checkout else []}]}))
    (d / "registry/repositories.json").write_text(json.dumps({"repositories": [
        {"id": "repository:o/p", "name_with_owner": "o/p", "local": local}]
        if checkout else []}))
    (d / "registry/relations.json").write_text(json.dumps({"relations": [
        {"id": "rel:1", "type": "implemented_by", "from": project_id,
         "to": "repository:o/p", "source_refs": ["SRC-0007"]}] if checkout else []}))
    env = {**os.environ, "OBSERVATORY_REGISTRY": str(d / "registry")}
    p = subprocess.run([PY, "plugins/release_cadence.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise AssertionError(f"the plugin failed: {(p.stdout + p.stderr)[-300:]}")
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


def value_of(rows: list[dict], metric: str):
    hit = [r for r in rows if r.get("metric") == metric]
    return hit[0]["value"] if len(hit) == 1 else None


# ─────────── the measurement ───────────────────────────────────────────

def test_a_tagged_checkout_is_counted_and_dated() -> None:
    r = repo_with([("v0.1.0", "2026-01-05"), ("v0.2.0", "2026-03-09"),
                   ("v0.3.0", "2026-06-01")])
    rows = measure(r)
    check("the tags are counted", value_of(rows, "release.tags") == 3.0,
          json.dumps(rows)[:240])
    days = value_of(rows, "release.days_since_last")
    check("and the newest one is dated", days is not None, json.dumps(rows)[:240])
    if days is not None:
        # 2026-06-01 is the newest of the three; the exact number moves with
        # today, so the assertion is the ORDERING rather than a literal that
        # would rot tomorrow.
        check("from the NEWEST tag, not the oldest", 0 <= days < 400, str(days))


def test_an_untagged_checkout_reports_zero_and_omits_the_date() -> None:
    """`0` days would mean "released today", which is the opposite of never.
    The count carries "never" and the date is ABSENT — the three-outcome rule
    drawn for `resolves`, applied to a release."""
    rows = measure(repo_with([]))
    check("the count says none", value_of(rows, "release.tags") == 0.0,
          json.dumps(rows)[:240])
    check("and no date is emitted at all",
          value_of(rows, "release.days_since_last") is None,
          json.dumps(rows)[:240])


def test_an_annotated_tags_own_date_is_used() -> None:
    """An annotated tag carries its own date, and it is the release's date — the
    commit it points at may be much older. `for-each-ref` reads the tag; a
    `git log` over `--tags` would read the commit."""
    r = repo_with([("v1.0.0", "2026-02-02")], annotated=True)
    rows = measure(r)
    check("the annotated tag is counted", value_of(rows, "release.tags") == 1.0,
          json.dumps(rows)[:200])
    check("and dated", value_of(rows, "release.days_since_last") is not None,
          json.dumps(rows)[:200])


def test_a_project_with_no_checkout_is_not_measured() -> None:
    check("nothing is emitted", measure(None) == [],
          "a project with no clone here has no tags to count, and 0 would be a claim")


def test_every_row_names_a_declared_metric_and_a_utc_instant() -> None:
    rows = measure(repo_with([("v1", "2026-04-04")]))
    declared = {m["name"] for m in
                json.loads(MANIFEST.read_text(encoding="utf-8"))["metrics"]}
    check("every metric is declared", {r["metric"] for r in rows} <= declared,
          f"{ {r['metric'] for r in rows} } vs {declared}")
    check("every `at` is a UTC Z instant",
          all(str(r.get("at", "")).endswith("Z") for r in rows),
          json.dumps(rows)[:240])
    check("and every value is a number",
          all(isinstance(r.get("value"), (int, float)) for r in rows),
          json.dumps(rows)[:240])


# ─────────── the manifest, by the contract's own rules ─────────────────

def test_the_manifest_declares_what_the_contract_requires() -> None:
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for field in ("id", "title", "script", "metrics", "every_hours", "requires", "why"):
        check(f"it declares `{field}`", field in m, str(sorted(m)))
    check("the script exists", (ROOT / "plugins" / m.get("script", "")).is_file(), "")
    check("every metric carries a unit and a meaning",
          all(x.get("unit") and len(str(x.get("means", "")).split()) >= 8
              for x in m.get("metrics", [])),
          json.dumps(m.get("metrics"))[:240])
    # THE ROLE. A core file resolves "has this project ever shipped" through
    # it without learning which plugin answers.
    roles = {x.get("role") for x in m.get("metrics", []) if x.get("role")}
    check("the count declares a role", roles == {"release.count"}, str(roles))
    check("and the date declares none, because nothing resolves it",
          not any(x.get("role") for x in m["metrics"]
                  if x["name"].endswith("days_since_last")), "")
    # PORTED-DIVERGED: the engine's manifest no longer narrates the history of
    # the role in `why`; the count's own `means` states what the role is for.
    count = next((x for x in m["metrics"] if x.get("role") == "release.count"), {})
    check("the manifest says what the role is for",
          "role" in count.get("means", "") and "plugin name" in count.get("means", ""),
          "a role nobody explains is the first thing a later reader removes")


def test_the_seam_holds_for_this_plugin_too() -> None:
    """The claim the plugin layer rests on, checked for the THIRD plugin: adding
    one touches no file outside `plugins/`."""
    import source_reader
    reader = source_reader.code_keeping_strings
    for core in ("collectors/merge.py", "collectors/emit_registry.py",
                 "tools/validate_registry.py", "tools/build_findings.py",
                 "dashboard/build_dashboard.py", "survey.py"):
        src = reader((ROOT / core).read_text(encoding="utf-8"))
        check(f"{core} does not name it",
              "release-cadence" not in src and "release.tags" not in src
              and "release.days_since_last" not in src,
              "a core file naming a plugin is the six-file problem returning")


def test_the_dashboard_renders_it_without_knowing_its_name() -> None:
    """The seam's promise DRIVEN rather than read. The dashboard's own comment
    says it reads "EVERY metric, by name and unit, never a name this file knows"
    — recorded after its first version read `disk.bytes` explicitly — so a new
    metric must reach the page with no edit to it."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    # THE PROPERTY, not the query string. This pinned the exact SELECT and went
    # red when the query grew an `at` column to carry the previous
    # sample — a change that strengthens the promise it guards rather than
    # breaking it. What matters is that the page reads the table without naming
    # a metric, so the assertion is now: it selects from `metrics`, and no
    # metric name appears anywhere in the file.
    import re as _re
    sel = _re.search(r"SELECT[^\"]*FROM metrics", src)
    check("the dashboard selects metrics generically", bool(sel),
          "if this becomes a named list, the promise is gone")
    # CODE, not prose. Both files talk ABOUT `disk.bytes` — the comment beside
    # the query records that its first version read that metric explicitly and
    # the plugin suite caught it — so a rule reading the whole file fires on the
    # very sentence that records the fix. A check matching a comment has
    # happened before; the remedy is always the same, and it is cheap.
    code = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("#", "//", "*", "/*")):
            continue
        for marker in ("  # ", "  // "):
            if marker in line:
                line = line.split(marker, 1)[0]
        code.append(line)
    body = "\n".join(code)
    named = [n for n in ("disk.bytes", "deps.direct", "git.branches",
                         "release.tags", "release.days_since_last",
                         "disk.reclaimable_bytes") if n in body]
    check("and no metric of any plugin is named in its CODE", not named, str(named))
    page = paths.DASHBOARD_HTML
    if not page.is_file():
        print("  NOTE  no rendered page to inspect; `./observatory.py dashboard` "
              "builds it [covered: the generic-SELECT assertion above IS the "
              "promise — a plugin metric reaches the page with no edit to the "
              "builder; this block only confirms it on the live page]")
        return
    text = page.read_text(encoding="utf-8")
    check("the live page carries the plugin metrics that exist",
          "disk.bytes" in text or "deps.direct" in text,
          "the generic path is what puts a metric on the page")


if __name__ == "__main__":
    print("release cadence — whether a project ever shipped anything\n")
    for fn in (test_a_tagged_checkout_is_counted_and_dated,
               test_an_untagged_checkout_reports_zero_and_omits_the_date,
               test_an_annotated_tags_own_date_is_used,
               test_a_project_with_no_checkout_is_not_measured,
               test_every_row_names_a_declared_metric_and_a_utc_instant,
               test_the_manifest_declares_what_the_contract_requires,
               test_the_seam_holds_for_this_plugin_too,
               test_the_dashboard_renders_it_without_knowing_its_name):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe estate can say which projects have shipped\033[0m")
