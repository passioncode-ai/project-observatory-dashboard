#!/usr/bin/env python3
"""The two plugins no test named — `git_branches` and `heroku_cost` — driven on
planted inputs.

Both are run the way `collectors/run_plugins.py` runs them: as a process with
the registry and the estate redirected, printing one JSON row per sample. What
each must NOT print is the point — a project with no readable checkout has no
branch count (absent, not zero), and an application linked to no project is
not a cost row (that money is `heroku.orphan_app`'s).
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def rows(p: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip().startswith("{")]


def git(repo: pathlib.Path, *cmd: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True, env=env, timeout=60, check=True)


def test_git_branches_counts_what_git_says_and_stays_silent_where_it_cannot() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plugpair-"))
    data, reg = d / "DATA", d / "registry"
    data.mkdir(); reg.mkdir()
    repo = data / "two-branches"
    repo.mkdir()
    git(repo, "init", "-q"); (repo / "f").write_text("x"); git(repo, "add", "f"); git(repo, "commit", "-qm", "seed")
    git(repo, "branch", "feature")
    (data / "not-a-repo").mkdir()
    (reg / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:two", "name": "two", "local_folders": ["two-branches"]},
        {"id": "project:none", "name": "none", "local_folders": ["not-a-repo"]},
        {"id": "project:gone", "name": "gone", "local_folders": ["missing"]},
    ]}), encoding="utf-8")
    p = subprocess.run([PY, str(ROOT / "plugins/git_branches.py")], cwd=ROOT,
                       env={**os.environ, "OBSERVATORY_REGISTRY": str(reg), "OBSERVATORY_DATA": str(data)},
                       capture_output=True, text=True, timeout=120)
    out = {r["project_id"]: r for r in rows(p)}
    check("the plugin exits 0", p.returncode == 0, p.stderr[-200:])
    check("a checkout with two branches reports 2.0 under the declared metric",
          out.get("project:two", {}).get("value") == 2.0 and out["project:two"]["metric"] == "git.branches", str(out))
    check("a folder git will not answer for has NO row — absent, not zero",
          "project:none" not in out and "project:gone" not in out, str(sorted(out)))


def test_heroku_cost_sums_per_project_and_skips_the_unlinked() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-plugpair-"))
    reg = d / "registry"; reg.mkdir()
    (reg / "heroku-apps.json").write_text(json.dumps({"apps": [
        {"name": "a1", "project": "project:p", "monthly_cost": 7.0, "scaled": 1},
        {"name": "a2", "project": "project:p", "monthly_cost": 25.0, "scaled": 2},
        {"name": "zero", "project": "project:q", "monthly_cost": 0, "scaled": 0},
        {"name": "orphan", "project": None, "monthly_cost": 50.0, "scaled": 1},
    ]}), encoding="utf-8")
    p = subprocess.run([PY, str(ROOT / "plugins/heroku_cost.py")], cwd=ROOT,
                       env={**os.environ, "OBSERVATORY_REGISTRY": str(reg)},
                       capture_output=True, text=True, timeout=120)
    got = {(r["project_id"], r["metric"]): r["value"] for r in rows(p)}
    check("the plugin exits 0", p.returncode == 0, p.stderr[-200:])
    check("cost and dynos are summed per project",
          got.get(("project:p", "heroku.cost_usd_month")) == 32.0 and got.get(("project:p", "heroku.dynos")) == 3.0, str(got))
    check("a project scaled to zero still has a row — 0 measured is a claim",
          got.get(("project:q", "heroku.cost_usd_month")) == 0.0, str(got))
    check("an application linked to no project is not a row here",
          not any(k[0] is None or k[0] == "None" for k in got), str(sorted(got)))
    check("the day, not the instant: every `at` is midnight UTC",
          all(r["at"].endswith("T00:00:00Z") for r in rows(p)), str([r["at"] for r in rows(p)][:2]))
    empty = d / "empty"; empty.mkdir()
    p2 = subprocess.run([PY, str(ROOT / "plugins/heroku_cost.py")], cwd=ROOT,
                        env={**os.environ, "OBSERVATORY_REGISTRY": str(empty)},
                        capture_output=True, text=True, timeout=120)
    check("no Heroku scan means no rows and exit 0, never a zero", p2.returncode == 0 and not rows(p2), p2.stdout[-100:])


def test_both_are_declared_and_their_metrics_are_the_ones_they_print() -> None:
    for manifest, script, metrics in (("git-branches.json", "git_branches.py", {"git.branches"}),
                                      ("heroku-cost.json", "heroku_cost.py", {"heroku.cost_usd_month", "heroku.dynos"})):
        doc = json.loads((ROOT / "plugins" / manifest).read_text(encoding="utf-8"))
        check(f"{manifest} names {script} and declares exactly what it prints",
              doc.get("script") == script and {m["name"] for m in doc.get("metrics", [])} == metrics,
              str(doc.get("metrics")))


if __name__ == "__main__":
    print("two plugins, driven on planted inputs — and what each must not print\n")
    for fn in (test_git_branches_counts_what_git_says_and_stays_silent_where_it_cannot,
               test_heroku_cost_sums_per_project_and_skips_the_unlinked,
               test_both_are_declared_and_their_metrics_are_the_ones_they_print):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mno plugin in this estate runs without a test naming it\033[0m")
