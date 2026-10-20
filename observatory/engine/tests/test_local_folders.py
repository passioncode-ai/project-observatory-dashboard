#!/usr/bin/env python3
"""A folder under the projects source reaches the registry by some anchor.

The scan → merge → emit pipeline runs on a synthetic workspace: a Git checkout
with commits and no remote, a plain folder, and a folder the workspace excludes
by name. Nothing outside the temporary directory is read.

This file is also the guard `tools/trap_efficacy.py` drives for T31: the
mutation there restores the source defect in `collectors/merge.py` and this
function must turn red, so it is written as a module-level function recording
into `FAILURES`, the shape that tool's driver calls.
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _environment(base: pathlib.Path) -> dict[str, str]:
    """Only what the collectors need: no inherited workspace, credential or Git config."""
    user = base / "user"
    user.mkdir()
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(user),
            "LANG": "C.UTF-8", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "OBSERVATORY_HOME": str(base / "home"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}


def _git_checkout_without_remote(folder: pathlib.Path, env: dict[str, str]) -> None:
    folder.mkdir()
    subprocess.run(["git", "init", "--template=", str(folder)], env=env,
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(folder), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=" + os.devnull,
                    "commit", "--allow-empty", "-m", "Synthetic fixture"], env=env,
                   check=True, capture_output=True)


def _run(env: dict[str, str], *argv: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, input=input,
                          capture_output=True, text=True, timeout=120)


def test_t31_a_folder_on_disk_is_never_silently_absent() -> None:
    """A remoteless Git checkout and a plain folder survive scan/merge/emit.
    Trap: T31

    The original defect skipped every Git folder in the local-only pass, so a
    checkout with commits and no remote left the registry with nothing
    reporting the loss: the repository pass needs a remote to derive an owner
    and name, and the local pass had already excluded it. Controlled disk
    inputs keep this independent of whatever else exists on a machine.
    """
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-t31-")).resolve()
    env = _environment(base)
    home = base / "home"
    projects = base / "projects"
    projects.mkdir()
    _git_checkout_without_remote(projects / "fixture-local", env)
    # A remote on a host this engine does not inventory (not GitHub, not
    # Bitbucket): published, just not listed here. It used to read "no remote at
    # all" and raise repo.no_remote.
    _git_checkout_without_remote(projects / "fixture-elsewhere", env)
    subprocess.run(["git", "-C", str(projects / "fixture-elsewhere"), "remote", "add", "origin",
                    "https://git.example.invalid/team/fixture-elsewhere.git"], env=env,
                   check=True, capture_output=True)
    (projects / "fixture-plain").mkdir()
    (projects / "fixture-plain" / "README.md").write_text("Synthetic plain folder\n")
    (projects / "fixture-excluded").mkdir()

    init = _run(env, "observatory.py", "init")
    check("T31 the synthetic workspace initialises", init.returncode == 0, init.stderr[-300:])
    if init.returncode:
        return
    settings = home / "config" / "settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("sources", {})["projects"] = str(projects)
    settings.write_text(json.dumps(doc), encoding="utf-8")
    exclusions = home / "config" / "folder_exclusions.json"
    rules = json.loads(exclusions.read_text(encoding="utf-8"))
    rules.setdefault("names", []).append(
        {"name": "fixture-excluded", "why": "This fixture is a container rather than a project."})
    exclusions.write_text(json.dumps(rules), encoding="utf-8")

    raw = home / "store" / "raw"  # paths-check: allow — the synthetic workspace's scratch, which each child resolves as its own paths.SCRATCH from OBSERVATORY_HOME
    for script, argument in (("collectors/scan_filesystem.py", str(raw / "local.json")),
                             ("collectors/merge.py", None),
                             ("collectors/emit_registry.py", None),
                             ("tools/validate_registry.py", None)):
        result = _run(env, script, *([argument] if argument else []))
        check(f"T31 {script} runs on its own inputs", result.returncode == 0, result.stderr[-300:])
        if result.returncode:
            return
    rows = json.loads((home / "registry" / "projects.json").read_text(encoding="utf-8"))["projects"]
    local = {p["local_only"]["folder"]: p for p in rows if p.get("local_only")}
    check("T31 both observed local folders reach the registry",
          {"fixture-local", "fixture-plain"} <= set(local), str(sorted(local)))
    check("T31 the excluded container remains absent", "fixture-excluded" not in local,
          str(sorted(local)))
    unpublished = local.get("fixture-local", {})
    check("T31 a remote-less git folder says so in its rule",
          unpublished.get("local_only", {}).get("unpublished") is True
          and any("no remote" in rule for rule in unpublished.get("membership_rules", [])),
          str(unpublished.get("membership_rules")))
    elsewhere = local.get("fixture-elsewhere", {})
    check("a remote on another host is not 'no remote at all'",
          elsewhere.get("local_only", {}).get("unpublished") is False
          and not any("no remote" in rule for rule in elsewhere.get("membership_rules", [])),
          str(elsewhere.get("local_only")) + str(elsewhere.get("membership_rules")))
    check("and its rule names the host, never the whole address",
          any("git.example.invalid" in rule for rule in elsewhere.get("membership_rules", []))
          and not any("/team/" in rule for rule in elsewhere.get("membership_rules", [])),
          str(elsewhere.get("membership_rules")))
    plain = local.get("fixture-plain", {})
    check("T31 a plain folder is recorded as not a git repository",
          plain.get("local_only", {}).get("unpublished") is False, str(plain.get("local_only")))


def _configure_projects(home: pathlib.Path, projects: pathlib.Path) -> None:
    settings = home / "config" / "settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("sources", {})["projects"] = str(projects)
    settings.write_text(json.dumps(doc), encoding="utf-8")


def _declare(home: pathlib.Path, active: list[pathlib.Path], dismissed: list[pathlib.Path] | None = None) -> None:
    declarations = home / "config" / "tracked.json"
    declarations.write_text(json.dumps({
        "schema_version": 1,
        "local_folders": [{"path": str(path.resolve()), "added_by": "agent:session-start",
                           "added_at": "2026-10-17T00:00:00Z"} for path in active],
        "dismissed_local_folders": [{"path": str(path.resolve()), "removed_by": "operator",
                                      "removed_at": "2026-10-17T00:00:00Z"}
                                     for path in dismissed or []],
    }), encoding="utf-8")


def _pipeline(env: dict[str, str], home: pathlib.Path) -> None:
    raw = home / "store" / "raw"  # paths-check: allow — synthetic workspace scratch
    for script, argument in (("collectors/scan_filesystem.py", str(raw / "local.json")),
                             ("collectors/merge.py", None),
                             ("collectors/emit_registry.py", None),
                             ("tools/validate_registry.py", None)):
        result = _run(env, script, *([argument] if argument else []))
        check(f"declared folders: {script} exits 0", result.returncode == 0,
              result.stdout[-300:] + result.stderr[-300:])
        if result.returncode:
            return


def test_a_declared_external_checkout_materializes_through_the_ordinary_pipeline() -> None:
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-declared-local-")).resolve()
    env = _environment(base)
    home, projects = base / "home", base / "projects"
    projects.mkdir()
    external = base / "outside" / "fixture-external"
    external.parent.mkdir()
    _git_checkout_without_remote(external, env)
    init = _run(env, "observatory.py", "init")
    check("the declaration workspace initialises", init.returncode == 0, init.stderr[-300:])
    if init.returncode:
        return
    _configure_projects(home, projects)
    hook = _run(env, "tools/session_start.py", input=json.dumps({"cwd": str(external), "session_id": "s-local"}))
    check("SessionStart declares the external checkout before the pipeline",
          hook.returncode == 0 and "declared in config/tracked.json" in hook.stdout, hook.stdout + hook.stderr)
    before = (home / "config" / "settings.json").read_bytes()
    _pipeline(env, home)
    scan = json.loads((home / "store" / "raw" / "local.json").read_text(encoding="utf-8"))  # paths-check: allow — synthetic workspace scratch
    row = next((r for r in scan["folders"] if r.get("path") == str(external.resolve())), None)
    check("the exact declared path reaches local.json through scan_folder",
          row is not None and row.get("folder") == str(external.resolve())
          and row.get("display_name") == "fixture-external", str(row))
    check("an undeclared parent is never substituted for the checkout",
          all(r.get("path") != str(external.parent.resolve()) for r in scan["folders"]), str(scan["folders"]))
    registry = json.loads((home / "registry" / "projects.json").read_text(encoding="utf-8"))["projects"]
    project = next((p for p in registry if (p.get("local_only") or {}).get("path") == str(external.resolve())), None)
    check("merge and emit turn the same row into a readable local project",
          project is not None and project.get("name") == "fixture-external"
          and project.get("local_folders") == [str(external.resolve())], str(project))
    check("sources.projects is not broadened",
          (home / "config" / "settings.json").read_bytes() == before, "settings changed")


def test_declared_worktrees_dedupe_and_exclusions_and_removals_stay_out() -> None:
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-declared-dedupe-")).resolve()
    env = _environment(base)
    home, projects = base / "home", base / "projects"
    projects.mkdir()
    outside = base / "outside"
    outside.mkdir()
    main, worktree = outside / "fixture-main", outside / "fixture-worktree"
    _git_checkout_without_remote(main, env)
    subprocess.run(["git", "-C", str(main), "remote", "add", "origin",
                    "git@github.com:Some-Org/fixture.git"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "fixture-wt",
                    str(worktree)], env=env, check=True, capture_output=True)
    excluded, dismissed = outside / "fixture-excluded", outside / "fixture-dismissed"
    _git_checkout_without_remote(excluded, env)
    _git_checkout_without_remote(dismissed, env)
    init = _run(env, "observatory.py", "init")
    check("the dedupe workspace initialises", init.returncode == 0, init.stderr[-300:])
    if init.returncode:
        return
    _configure_projects(home, projects)
    _declare(home, [main, worktree, excluded], [dismissed])
    exclusions = home / "config" / "folder_exclusions.json"
    rules = json.loads(exclusions.read_text(encoding="utf-8"))
    rules.setdefault("names", []).append({"name": "fixture-excluded", "why": "Synthetic exclusion."})
    exclusions.write_text(json.dumps(rules), encoding="utf-8")
    _pipeline(env, home)
    scan = json.loads((home / "store" / "raw" / "local.json").read_text(encoding="utf-8"))  # paths-check: allow — synthetic workspace scratch
    paths = {r.get("path") for r in scan["folders"]}
    check("both clones are measured but the excluded and removed paths are not",
          str(main.resolve()) in paths and str(worktree.resolve()) in paths
          and str(excluded.resolve()) not in paths and str(dismissed.resolve()) not in paths, str(paths))
    check("the exclusion is reported, not silent",
          any(r.get("folder") == str(excluded.resolve()) for r in scan.get("skipped") or []), str(scan.get("skipped")))
    repositories = json.loads((home / "registry" / "repositories.json").read_text(encoding="utf-8"))["repositories"]
    matching = [r for r in repositories if r.get("name_with_owner") == "Some-Org/fixture"]
    check("one repository anchors the real checkout and keeps the worktree beside it",
          len(matching) == 1
          and any(c.get("path") == str(worktree.resolve())
                  for c in (matching[0].get("local") or {}).get("extra_checkouts") or []), str(matching))
    projects_out = json.loads((home / "registry" / "projects.json").read_text(encoding="utf-8"))["projects"]
    check("worktrees do not become sibling projects",
          len([p for p in projects_out if "fixture" in p.get("name", "")]) == 1, str(projects_out))


def test_a_remoteless_worktree_joins_its_main_checkout_instead_of_a_sibling_project() -> None:
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-declared-solo-")).resolve()
    env = _environment(base)
    home, projects = base / "home", base / "projects"
    projects.mkdir()
    outside = base / "outside"
    outside.mkdir()
    main, worktree = outside / "fixture-solo", outside / "fixture-solo-wt"
    _git_checkout_without_remote(main, env)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "fixture-solo-wt",
                    str(worktree)], env=env, check=True, capture_output=True)
    init = _run(env, "observatory.py", "init")
    check("the remoteless workspace initialises", init.returncode == 0, init.stderr[-300:])
    if init.returncode:
        return
    _configure_projects(home, projects)
    _declare(home, [main, worktree])
    _pipeline(env, home)
    projects_out = json.loads((home / "registry" / "projects.json").read_text(encoding="utf-8"))["projects"]
    solo = [p for p in projects_out if "fixture-solo" in p.get("name", "")]
    check("a worktree of a repository with no remote is not a second project",
          len(solo) == 1 and solo[0].get("name") == "fixture-solo", str([p.get("name") for p in projects_out]))
    check("the worktree is named in its project's membership rules, not lost",
          bool(solo) and any(str(worktree.resolve()) in r and "worktree of" in r
                             for r in solo[0].get("membership_rules") or []), str(solo))


def test_declaration_gaps_are_degraded_and_the_root_scan_survives() -> None:
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-declared-degraded-")).resolve()
    env = _environment(base)
    home, projects = base / "home", base / "projects"
    projects.mkdir()
    _git_checkout_without_remote(projects / "fixture-local", env)
    init = _run(env, "observatory.py", "init")
    check("the degraded workspace initialises", init.returncode == 0, init.stderr[-300:])
    if init.returncode:
        return
    _configure_projects(home, projects)
    declarations = home / "config" / "tracked.json"
    declarations.write_text(json.dumps({"schema_version": 1, "local_folders": [
        {"path": str((base / "outside" / "missing").resolve())},
        {"path": 42},
    ]}), encoding="utf-8")
    before = declarations.read_bytes()
    raw = home / "store" / "raw"  # paths-check: allow — synthetic workspace scratch
    result = _run(env, "collectors/scan_filesystem.py", str(raw / "local.json"))
    check("a missing or malformed declaration does not abort the filesystem scan",
          result.returncode == 0, result.stderr[-300:])
    scan = json.loads((raw / "local.json").read_text(encoding="utf-8"))
    check("the ordinary root child is still measured",
          any(r.get("folder") == "fixture-local" for r in scan["folders"]), str(scan["folders"]))
    check("each unusable declaration is named in degraded",
          any("missing" in d.get("source", "") for d in scan.get("degraded") or [])
          and any("declaration 1" in d.get("reason", "") for d in scan.get("degraded") or []),
          str(scan.get("degraded")))
    check("the scan never rewrites the declaration store", declarations.read_bytes() == before, "tracked.json changed")


if __name__ == "__main__":
    print("local folders — every folder under the projects source reaches the registry\n")
    test_t31_a_folder_on_disk_is_never_silently_absent()
    test_a_declared_external_checkout_materializes_through_the_ordinary_pipeline()
    test_declared_worktrees_dedupe_and_exclusions_and_removals_stay_out()
    test_a_remoteless_worktree_joins_its_main_checkout_instead_of_a_sibling_project()
    test_declaration_gaps_are_degraded_and_the_root_scan_survives()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("every observed folder has an anchor in the registry")
