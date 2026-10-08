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


def _run(env: dict[str, str], *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], cwd=ROOT, env=env,
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
                             ("collectors/emit_registry.py", None)):
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


if __name__ == "__main__":
    print("local folders — every folder under the projects source reaches the registry\n")
    test_t31_a_folder_on_disk_is_never_silently_absent()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("every observed folder has an anchor in the registry")
