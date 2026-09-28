#!/usr/bin/env python3
"""Observed activity, and why it is not `lifecycle`.

`lifecycle` answers a DECLARED question — has the owner marked this finished —
and in practice it is `archived` only when every repository carries GitHub's
`isArchived` flag. No staleness threshold exists there, so a project untouched
for a decade stays `lifecycle: active`: truthfully, because nobody archived it.
The registry could say what a project claimed and not what it was doing.

`activity_tier` is the other axis and stays a separate field, because a field
answering two questions answers neither: a project can be archived and have moved
last week, or unarchived and untouched for a decade, and both facts are true.

Everything here runs on a synthetic emitter model with planted dates, so the
distribution the assertions need is built rather than hoped for.
"""
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402
import activity                                                     # noqa: E402
import paths                                                        # noqa: E402
from emitter_fixture import seed, environment                       # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
TODAY = date(2026, 9, 7)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def ago(days: int, today: date = TODAY) -> str:
    return (today - timedelta(days=days)).isoformat()


def test_the_boundaries_are_where_the_config_says() -> None:
    cases = [(0, "active"), (30, "active"), (31, "cooling"), (180, "cooling"),
             (181, "dormant"), (730, "dormant"), (731, "cold"), (4000, "cold")]
    for days, want in cases:
        got = activity.tier_of(ago(days), today=TODAY)
        check(f"{days:5}d ago -> {want}", got == want, got)


def test_an_absent_date_is_its_own_answer() -> None:
    """Bucketing it with the oldest would assert an absence nobody measured."""
    for value in (None, "", "not-a-date"):
        check(f"{value!r} -> unknown", activity.tier_of(value, today=TODAY) == "unknown",
              activity.tier_of(value, today=TODAY))
    check("days_since says None rather than zero or infinity",
          activity.days_since(None) is None)
    check("`unknown` is a declared tier, not an accident",
          "unknown" in activity.tier_ids())


def emitted_estate() -> list[dict]:
    """Run the real emitter over a model whose projects span every tier.

    Two of the unarchived projects are years old: those are the rows where the
    declared axis (`lifecycle=active`) and the observed one (`cold`) disagree.
    """
    root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tier-emit-"))
    seed(root)
    model = root / "raw/model.json"
    doc = json.loads(model.read_text(encoding="utf-8"))
    template = doc["projects"]["fixture-a"]
    today = date.today()
    ages = {"alpha-web": 3, "beta-api": 90, "gamma-app": 400, "delta-site": 1200,
            "epsilon-tool": 3000, "zeta-lib": None}
    doc["projects"] = {name: dict(template, name=name.title(), repos=[],
                                  last_activity=None if age is None else ago(age, today))
                       for name, age in ages.items()}
    doc["repositories"] = {}
    model.write_text(json.dumps(doc), encoding="utf-8")
    p = subprocess.run([PY, str(ROOT / "collectors/emit_registry.py"), str(root / "raw")],
                       cwd=ROOT, env=environment(root), capture_output=True, text=True, timeout=120)
    check("the real emitter accepts the dated model", p.returncode == 0, p.stderr[-400:])
    if p.returncode:
        return []
    return json.loads((root / "registry/projects.json").read_text(encoding="utf-8"))["projects"]


def test_it_is_the_other_axis_from_lifecycle() -> None:
    projects = emitted_estate()
    check("every project carries a tier",
          bool(projects) and all(p.get("activity_tier") for p in projects),
          str([p["id"] for p in projects if not p.get("activity_tier")][:3]))
    tiers = {p.get("activity_tier") for p in projects}
    check("the estate uses more than one tier — a single value would say nothing",
          len(tiers) >= 4, str(sorted(map(str, tiers))))
    check("a project with no date is `unknown`, not the oldest bucket",
          any(p.get("last_activity_on") is None and p.get("activity_tier") == "unknown"
              for p in projects))
    # The two axes must be able to disagree, or one of them is redundant.
    quiet_but_live = [p for p in projects
                      if p.get("lifecycle") == "active" and p.get("activity_tier") == "cold"]
    check("projects exist that are lifecycle=active and observably cold",
          len(quiet_but_live) >= 2,
          "if the two axes never disagreed, one of them would be decoration")


def test_the_tier_is_derived_not_carried() -> None:
    src = (ROOT / "collectors/emit_registry.py").read_text(encoding="utf-8")
    check("the emitter derives it on every run", "activity.tier_of(" in src)
    check("and never falls back to the previous artefact",
          'prev.get("activity_tier"' not in src,
          "a tier carried forward is trap T4 in a new field")


def run_validator(reg: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "tools/validate_registry.py"], cwd=ROOT, capture_output=True,
                          text=True, timeout=300,
                          env={**os.environ, "OBSERVATORY_REGISTRY": str(reg)})


def test_the_validator_catches_a_typed_tier() -> None:
    """A tier that can be typed can disagree with the date beside it.

    The synthetic workspace registry is valid as built; dating two of its
    projects years ago with a consistent tier must keep it valid (the control),
    and then typing `active` over one of them must not.
    """
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tier-"))
    shutil.copytree(paths.REGISTRY, d / "registry")
    reg = d / "registry"
    doc = json.loads((reg / "projects.json").read_text(encoding="utf-8"))
    old = ago(4000, date.today())
    for p in doc["projects"][:2]:
        p["last_activity_on"] = old
        p["activity_tier"] = activity.tier_of(old)
    (reg / "projects.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    control = run_validator(reg)
    check("the dated registry with derived tiers is valid (control)", control.returncode == 0,
          (control.stdout + control.stderr)[-300:])
    doc["projects"][0]["activity_tier"] = "active"
    (reg / "projects.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    p = run_validator(reg)
    out = p.stdout + p.stderr
    check("claiming a quiet project is active fails validation", p.returncode != 0, out[-160:])
    check("and the message shows the date that contradicts it",
          "derives" in out and "last_activity_on" in out, out[-200:])


def test_a_boundary_project_does_not_fail_the_gate_at_midnight() -> None:
    """The tier depends on today, so a project exactly on a boundary changes
    tier between an emit at 23:59 and a validation at 00:01. A gate that goes
    red on a non-defect teaches people to ignore it."""
    src = (ROOT / "tools/validate_registry.py").read_text(encoding="utf-8")
    check("the validator allows one day of drift", "timedelta(days=1)" in src)
    edge = ago(30)
    check("a project on the boundary derives one tier today and another tomorrow",
          activity.tier_of(edge, today=TODAY) != activity.tier_of(edge, today=TODAY + timedelta(days=1)),
          "if this stops being true the tolerance is guarding nothing")


def test_the_dashboard_shows_it_as_a_word() -> None:
    """The engine renders the tier through its label table and the locale
    catalogs, where the private original had a Russian-only table."""
    src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the page carries the tier", '"tier": project.get("activity_tier"' in src)
    check("and renders it as a word, never a colour alone", "TIER_LABEL" in src)
    ru = json.loads((ROOT / "dashboard/locales/ru.json").read_text(encoding="utf-8"))
    check("every tier has a Russian word in the catalog",
          all(ru.get(t) for t in ("active", "cooling", "dormant", "cold")),
          str([t for t in ("active", "cooling", "dormant", "cold") if not ru.get(t)]))
    page = paths.DASHBOARD_HTML
    check("the synthetic workspace built a page", page.exists(), str(page))
    if page.exists():
        text = page.read_text(encoding="utf-8")
        check("the built page contains the tier vocabulary",
              "TIER_LABEL" in text and "cooling" in text)


if __name__ == "__main__":
    print("activity — what a project is doing, beside what it declares\n")
    for fn in (test_the_boundaries_are_where_the_config_says,
               test_an_absent_date_is_its_own_answer,
               test_it_is_the_other_axis_from_lifecycle,
               test_the_tier_is_derived_not_carried,
               test_the_validator_catches_a_typed_tier,
               test_a_boundary_project_does_not_fail_the_gate_at_midnight,
               test_the_dashboard_shows_it_as_a_word):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe registry now says what its projects are DOING\033[0m")
