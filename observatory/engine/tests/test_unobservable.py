#!/usr/bin/env python3
"""A project the system cannot observe is invisible, including that fact.

Found while checking whether the founding question — which projects are active —
is answered trustworthily. It is: the registry's `last_activity_on` and the
store's newest commit agreed for every project. But two projects carried
`activity_tier: unknown`, and reading them showed why:

    lifecycle active · ownership local-only
    local_folders []   repositories []   last_activity_on ""   has_vault_note True

Neither had a folder on the machine, a repository, or an activity date. The only
witness that they existed at all was a note in the wiki. So `activity_tier:
unknown` is HONEST — nothing could be measured — while `lifecycle: active` is a
claim with no evidence of any kind behind it.

**And nothing said so.** Neither reaches the findings board. The lost-projects
tool does not catch them either: it tracks projects whose FOLDER disappeared and
whose history it remembers, and these two never had a folder recorded, so there
is nothing for it to miss.

That is the blind spot of the whole system's premise. It watches projects; a
project it cannot watch produces no observation, and the ABSENCE of observation
was itself unobserved.

**The trigger is the evidence, not the tier.** `unknown` is a consequence — a
project could be `unknown` for other reasons, and testing the consequence would
make the rule fire on the wrong population. What the row tests is what can be
measured: no local folder, and no repository related to it.

**Info, not warning.** Nothing is broken and no work is at risk; what exists is a
declaration the registry cannot support. The operator either points it at
something observable or lets the lifecycle say what the evidence says.
"""
from __future__ import annotations
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
sys.path.insert(0, str(ROOT / "tools"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rule(projects, related):
    import build_findings as B
    fn = getattr(B, "unobservable_projects", None)
    return None if fn is None else fn(projects, related)


NOTE_ONLY = {"id": "project:ghost", "name": "ghost", "lifecycle": "active",
             "ownership": "local-only", "local_folders": [],
             "last_activity_on": "", "has_vault_note": True,
             "activity_tier": "unknown"}
WITH_FOLDER = dict(NOTE_ONLY, id="project:real", name="real",
                   local_folders=["real"], activity_tier="active")


# ─────────── the rule tests the evidence ───────────────────────────────

def test_a_project_with_no_folder_and_no_repository_is_reported() -> None:
    out = rule([NOTE_ONLY], set())
    if out is None:
        check("build_findings.unobservable_projects exists", False,
              "a project the system cannot watch reached no surface at all")
        return
    check("it produces one row", len(out) == 1, str(out))
    if not out:
        return
    f = out[0]
    check("as info — nothing is broken", f["severity"] == "info", f["severity"])
    check("the subject is the project", f["subject"] == "project:ghost", f["subject"])
    blob = json.dumps(f, ensure_ascii=False)
    check("it says the wiki is the only witness",
          "вики" in blob or "wiki" in blob.lower(), f["detail"][:200])
    check("and names the lifecycle the evidence cannot support",
          "active" in f["detail"], f["detail"][:200])


def test_a_folder_is_enough_to_be_observable() -> None:
    out = rule([WITH_FOLDER], set())
    if out is None:
        return
    check("a project with a folder is not reported", out == [], str(out))


def test_a_repository_is_enough_too() -> None:
    out = rule([NOTE_ONLY], {"project:ghost"})
    if out is None:
        return
    check("a project with a related repository is not reported", out == [], str(out))


def test_the_trigger_is_the_evidence_not_the_tier() -> None:
    """`unknown` is a consequence. Testing it would make the rule fire on
    whatever else happens to be unknown, and miss an unobservable project whose
    tier was computed as something else."""
    out = rule([dict(NOTE_ONLY, activity_tier="cold")], set())
    if out is None:
        return
    check("an unobservable project is reported whatever its tier",
          len(out) == 1, str(out))
    # THROUGH `code_only`, which blanks comments AND string literals. The first
    # version searched the raw source and failed on the rule's own docstring and
    # on the prose in its detail — an assertion matching what the code SAYS
    # about a thing rather than what it does with it, for the seventh time this
    # session. The shared reader exists precisely for this.
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_only(
        (ROOT / "tools/build_findings.py").read_text(encoding="utf-8"))
    i = code.find("def unobservable_projects")
    body = code[i:code.find("\ndef ", i + 10)] if i != -1 else ""
    check("and the rule does not branch on activity_tier",
          "activity_tier" not in body,
          "the tier is what this explains, not what selects it")


def test_no_projects_means_no_rows() -> None:
    out = rule([], set())
    if out is None:
        return
    check("an empty estate reports nothing", out == [], str(out))


# ─────────── the live estate ───────────────────────────────────────────

def test_the_two_live_ones_are_named() -> None:
    import build_findings as B
    rows = [f for f in B.collect() if f["type"] == "project.unobservable"]
    import paths
    projs = json.loads(
        (paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]
    rels = json.loads(
        (paths.REGISTRY / "relations.json").read_text(encoding="utf-8"))["relations"]
    has_repo = {r["from"] for r in rels if r["to"].startswith("repository:")}
    blind = [p["id"] for p in projs
             if not (p.get("local_folders") or []) and p["id"] not in has_repo]
    if not blind:
        print("  NOTE  every project on this estate is observable "
              "[covered: the fixture cases above]")
        return
    check(f"each of the {len(blind)} unobservable project(s) has a row",
          {f["subject"] for f in rows} == set(blind),
          f"rows {sorted(f['subject'] for f in rows)} vs blind {sorted(blind)}")


def test_a_declaration_that_disagrees_with_the_measurement_is_counted():
    """`project.declared_alive_measured_dead` — S2's question as a number.

    Two facts, not one: `lifecycle` is declared and `activity_tier` is measured,
    and the screen must show both. What nothing did was COUNT the gap, so it
    tripled without anyone being told.
    """
    import build_findings as B
    # The type string is written out because `tests/test_finding_rules.py`
    # requires every rule to be NAMED by some suite: a rule exercised through a
    # function reference is one nobody can find from the other direction.
    assert "project.declared_alive_measured_dead"
    P = [{"name": "moving", "lifecycle": "active", "activity_tier": "active"},
         {"name": "stopped", "lifecycle": "active", "activity_tier": "dormant"},
         {"name": "colder", "lifecycle": "active", "activity_tier": "cold"},
         {"name": "honest", "lifecycle": "archived", "activity_tier": "cold"}]
    rows = B.declared_alive_measured_dead(P)
    check("one aggregate row, never one per project", len(rows) == 1,
          "forty rows of one sentence is the clone.stale defect again")
    check("only the projects that disagree are counted", "2 projects" in rows[0]["title"],
          rows[0]["title"])
    check("a project whose declaration matches its measurement is not counted",
          "moving" not in rows[0]["detail"] and "honest" not in rows[0]["detail"])
    check("agreement everywhere raises nothing",
          B.declared_alive_measured_dead([P[0], P[3]]) == [])
    check("and the row carries the type the board indexes it by",
          rows[0]["type"] == "project.declared_alive_measured_dead")


if __name__ == "__main__":
    print("unobservable — a project the system cannot watch said nothing\n")
    for fn in (test_a_project_with_no_folder_and_no_repository_is_reported,
               test_a_folder_is_enough_to_be_observable,
               test_a_repository_is_enough_too,
               test_the_trigger_is_the_evidence_not_the_tier,
               test_no_projects_means_no_rows,
               test_the_two_live_ones_are_named,
               test_a_declaration_that_disagrees_with_the_measurement_is_counted):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe absence of an observation is observed\033[0m")
