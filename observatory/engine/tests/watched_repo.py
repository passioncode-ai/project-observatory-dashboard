#!/usr/bin/env python3
"""A watched repository the fixtures OWN, instead of the one they run inside.

`tools/record_turn.py` records a turn when the checkout it is pointed at has
uncommitted files or unpushed commits, and three suites pointed it at THIS
repository. That worked for as long as the tree was dirty — the work was in
progress — and it stopped working the moment the tree was clean and pushed,
which is the state the project is supposed to be in: three suites red, no code
changed.

That is trap T16 exactly — *a fixture that depends on ambient state is not a
fixture* — and the remedy is the one already applied to the store: the fixture
builds its own subject. Here that means a real git repository with a real
change, plus a registry that names it, because the hook refuses anything the
registry does not know:

    repo_id_for()  matches by remote nwo, or by `local.path`
    project_for()  needs an `implemented_by` relation to a project
    estate         records only ownership in RECORDED_OWNERSHIP

So the helper writes all three. Nothing here touches the operator's registry or
the operator's checkout, and the returned environment redirects the store, the
scratch and the registry.
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

#: The ownership the estate records. Read from the rule rather than spelled
#: again — `estate.py` exists because two readers of it once disagreed.
sys.path.insert(0, str(ROOT))
import estate                                                       # noqa: E402
OWNERSHIP = sorted(estate.RECORDED_OWNERSHIP)[0]


def _git(cwd: pathlib.Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    return p.stdout


def build(dirty: bool = True) -> tuple[pathlib.Path, pathlib.Path, dict]:
    """(workspace, checkout, env) — a git repo the registry knows, with a change.

    `dirty=False` builds the same repository with nothing to record, which is
    the other half a fixture usually wants: the hook must stay silent then, and
    saying so requires a subject that is genuinely clean rather than a
    repository that happens to be.
    """
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-watched-"))
    repo = work / "checkout"
    repo.mkdir()
    (work / "scratch").mkdir()
    reg = work / "registry"
    reg.mkdir(exist_ok=True)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "README.md").write_text("a watched project\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first")
    if dirty:
        # AN UNCOMMITTED FILE, which is what the hook calls a change. A commit
        # would need an upstream to be "unpushed" against, and inventing a
        # remote would make the fixture about `@{u}` rather than about the hook.
        (repo / "worked-on.txt").write_text("the turn's work\n", encoding="utf-8")

    top = _git(repo, "rev-parse", "--show-toplevel").strip()
    repo_id = "repository:fixture/watched"
    project_id = "project:watched"
    (reg / "repositories.json").write_text(json.dumps({
        "schema_version": 1,
        "repositories": [{"id": repo_id, "name_with_owner": "fixture/watched",
                          "host": "github", "local": {"path": top}}]},
        ensure_ascii=False), encoding="utf-8")
    (reg / "projects.json").write_text(json.dumps({
        "schema_version": 1,
        "projects": [{"id": project_id, "name": "watched", "ownership": OWNERSHIP,
                      "anchor": "local-folder", "membership_rules": []}]},
        ensure_ascii=False), encoding="utf-8")
    (reg / "relations.json").write_text(json.dumps({
        "schema_version": 2,
        "relations": [{"id": "relation:watched:implemented-by:fixture-watched",
                       "type": "implemented_by", "from": project_id, "to": repo_id,
                       "source_refs": []}]},
        ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ,
               OBSERVATORY_DB=str(work / "observatory.db"),
               OBSERVATORY_SCRATCH=str(work / "scratch"),
               OBSERVATORY_REGISTRY=str(reg))
    return work, repo, env


def record(env: dict, session: str, cwd: pathlib.Path) -> subprocess.CompletedProcess:
    """Drive the recorder at the fixture's own checkout."""
    py = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
    return subprocess.run([py, "tools/record_turn.py", "--cwd", str(cwd),
                           "--session-id", session],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


if __name__ == "__main__":                                   # a smoke of the helper
    work, repo, env = build()
    got = record(env, "s-smoke", repo)
    print(got.stdout.strip() or got.stderr.strip())
