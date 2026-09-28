#!/usr/bin/env python3
"""A composition written into a manifest could not discriminate, and the measurement said so.

The release-cadence manifest once claimed the metric "composes with what is
already measured: never released, plus active, plus commits no remote has, is a
project worth looking at". **Measured on a real portfolio, that conjunction
selected most of the projects holding unpushed work, so it discriminated
nothing**, and the
reason is worse than the number: `activity_tier` is derived from
`last_activity_on`, which is derived from the commits that make a clone AHEAD of
its remote. A project holding unpushed commits is `active` BY CONSTRUCTION. Two
of the three terms were one measurement wearing two names, written into a
manifest as if it were a useful conjunction.

**The cross-tab that does discriminate is the opposite cell**, and there the
terms are independent — a tag count and a commit recency can diverge without
limit. On that portfolio, never-shipped-and-active was the largest cell and
never-shipped-and-quiet (cooling, dormant, cold) the next.

**Those projects consumed effort, released nothing, and have stopped.** That was
a fifth of the measured portfolio and it is the archive-or-revive decision the
brief asks the observatory to support — and nothing reported it:
`tools/build_findings.py` mentions neither `activity_tier` nor `lifecycle`, so a
tier is computed, validated, drawn on the dashboard and never turned into a
question.

Four boundaries this finding holds, each of them a lesson already paid for here:

* **One row for the lot.** One row per project would be the noise the board
  already removed from the unattributed list.
* **`info`, not a warning.** A standing portfolio state that cannot be cleared
  quickly becomes furniture at warning level — the same reasoning the board
  applies to stale clones.
* **An archived project is left alone.** `lifecycle: archived` is the operator
  having already decided, and a finding that re-asks a settled question teaches
  that deciding does nothing.
* **Never MEASURED is not never SHIPPED.** A project with no `release.tags`
  reading is absent from the set rather than counted as unreleased — the same
  rule the board applies to an unmeasured `resolves`.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

# A private workspace of this suite's own, so the board reads the shipped
# default configuration rather than a machine's; each estate below redirects
# the registry, scratch and store to its own planted copies.
for _name in ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH"):
    os.environ.pop(_name, None)
os.environ["OBSERVATORY_HOME"] = str(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-portfolio-home-")).resolve() / "home")
subprocess.run([PY, str(ROOT / "observatory.py"), "init"], cwd=ROOT,
               capture_output=True, timeout=120, check=True)
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def estate(projects: list[dict], tags: dict[str, float | None]) -> list[dict]:
    """Findings for a planted portfolio. `tags` maps project id -> release.tags,
    and a project absent from it has no reading at all."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-portfolio-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text(json.dumps({"projects": projects}))
    (d / "registry/repositories.json").write_text('{"repositories": []}')
    (d / "registry/relations.json").write_text('{"relations": []}')
    db = d / "observatory.db"
    env = dict(os.environ, OBSERVATORY_DB=str(db),
               OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    con = sqlite3.connect(db)
    for pid, value in tags.items():
        if value is None:
            continue
        # `recorded_at` as well as `at`: the first is when the row was WRITTEN
        # and the second the instant it describes, and the schema requires both.
        con.execute("INSERT INTO metrics (project_id, metric, unit, value, at,"
                    " source, recorded_at) VALUES (?,?,?,?,?,?,?)",
                    (pid, "release.tags", "tags", value, "2026-09-07T00:00:00Z",
                     "release-cadence", "2026-09-07T00:00:00Z"))
    con.commit()
    con.close()
    for k, v in env.items():
        if k.startswith("OBSERVATORY_"):
            os.environ[k] = v
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)
    try:
        return [f for f in B.collect() if f["type"].startswith("portfolio.")]
    finally:
        for k in ("OBSERVATORY_DB", "OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH"):
            os.environ.pop(k, None)
        importlib.reload(paths)


def proj(pid: str, tier: str, lifecycle: str = "active", last: str = "2026-05-01") -> dict:
    return {"id": pid, "name": pid.split(":", 1)[1], "anchor": "vault-folder",
            "ownership": "owned", "lifecycle": lifecycle, "activity_tier": tier,
            "last_activity_on": last, "local_folders": [], "membership_rules": [],
            "sites": [], "stack": []}


def one(got: list[dict]) -> dict | None:
    return got[0] if len(got) == 1 else None


# ─────────── the cell that discriminates ───────────────────────────────

def test_a_quiet_unreleased_project_is_raised_once_for_the_lot() -> None:
    got = estate([proj("project:a", "cooling"), proj("project:b", "dormant"),
                  proj("project:c", "cold")],
                 {"project:a": 0.0, "project:b": 0.0, "project:c": 0.0})
    f = one(got)
    check("one finding, not three", f is not None, str([x["title"] for x in got]))
    if f is None:
        return
    check("as info — a standing state at warning level becomes furniture",
          f["severity"] == "info", f["severity"])
    check("the count is in the title", "3" in f["title"], f["title"])
    check("and the projects are named", "project:a" in f["detail"], f["detail"][:240])
    check("the decision is named, not a remedy",
          "archive" in f["action"].lower(), f["action"])


def test_an_active_project_is_not_in_it() -> None:
    """The cell that does NOT discriminate: most projects are active and have
    never shipped, which is the portfolio's shape rather than a question."""
    check("active and unreleased says nothing",
          estate([proj("project:a", "active")], {"project:a": 0.0}) == [],
          "most projects are in that state; a finding on it would be a finding on "
          "the way the operator works")


def test_a_project_that_shipped_is_not_in_it() -> None:
    check("shipped and quiet says nothing",
          estate([proj("project:a", "cooling")], {"project:a": 7.0}) == [],
          "a released project going quiet is a project that finished")


def test_an_archived_project_is_left_alone() -> None:
    """`lifecycle: archived` is the operator having already decided, and
    re-asking a settled question teaches that deciding does nothing."""
    check("an archived project is not asked about again",
          estate([proj("project:a", "dormant", lifecycle="archived")],
                 {"project:a": 0.0}) == [],
          "")


def test_never_measured_is_not_never_shipped() -> None:
    """A project with no reading is absent from the set, not
    counted as unreleased — the plugin only measures projects with a checkout
    here, so a project with none would otherwise be reported as having shipped
    nothing."""
    check("a project with no release reading is left out",
          estate([proj("project:a", "dormant")], {}) == [],
          "no reading is not a zero")


def test_the_longest_quiet_come_first_and_the_list_says_what_it_omits() -> None:
    projects = [proj(f"project:p{i}", "cooling", last=f"2026-0{1 + i % 8}-01")
                for i in range(9)]
    got = estate(projects, {p["id"]: 0.0 for p in projects})
    f = one(got)
    check("it fires", f is not None, str(got)[:200])
    if f is None:
        return
    check("the count is all nine", "9" in f["title"], f["title"])
    check("the detail is bounded", len(f["detail"]) < 900, str(len(f["detail"])))
    check("and says how many it did not name",
          "more" in f["detail"] or "not shown" in f["detail"], f["detail"][-200:])
    check("the quietest is named first",
          f["detail"].index("p0") < f["detail"].index("p7")
          if "p0" in f["detail"] and "p7" in f["detail"] else True,
          f["detail"][:240])


def test_an_empty_portfolio_is_silent() -> None:
    check("nothing to review, nothing said", estate([], {}) == [], "")


# ─────────── the manifest's own claim, corrected ───────────────────────

def test_the_manifests_composition_claim_is_corrected() -> None:
    """The manifest once named a conjunction whose terms are not independent.
    Leaving it would be a document asserting a reading the data refutes, which
    this repository treats as worse than silence."""
    m = json.loads((ROOT / "plugins/release-cadence.json").read_text(encoding="utf-8"))
    why = m["why"]
    check("it no longer offers the unsound conjunction",
          "plus active, plus commits no remote has" not in why, why[-300:])
    # PORTED-DIVERGED: the public manifest words the correction as the
    # independence of the two measurements rather than telling the history of
    # the refuted claim, so the assertions read that wording.
    check("and it names the cell that does discriminate",
          "inactive projects that never tagged" in why.lower(), why[-300:])
    check("with the reason it discriminates: the measurements are independent",
          "independent" in why.lower(),
          "a corrected claim without its reason invites the same error")


if __name__ == "__main__":
    print("portfolio review — effort in, nothing out, and stopped\n")
    for fn in (test_a_quiet_unreleased_project_is_raised_once_for_the_lot,
               test_an_active_project_is_not_in_it,
               test_a_project_that_shipped_is_not_in_it,
               test_an_archived_project_is_left_alone,
               test_never_measured_is_not_never_shipped,
               test_the_longest_quiet_come_first_and_the_list_says_what_it_omits,
               test_an_empty_portfolio_is_silent,
               test_the_manifests_composition_claim_is_corrected):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe portfolio's unfinished, stopped projects have a question attached\033[0m")
