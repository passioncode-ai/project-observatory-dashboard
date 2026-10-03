#!/usr/bin/env python3
"""The one line an agent gets when it opens a project — driven on a
planted registry and a planted estate, so every branch is watched: a known
folder, an unknown folder inside the estate, a checkout outside it, a directory
that is not a repository, and the observatory itself. The rule under test is
the rule the hook has always followed: at most two lines, only when
actionable, exit 0 always, never a value.
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
GIT = "/opt/homebrew/bin/git" if pathlib.Path("/opt/homebrew/bin/git").exists() else "git"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def git(repo: pathlib.Path, *cmd: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
    subprocess.run([GIT, "-C", str(repo), *cmd], capture_output=True, env=env, timeout=60, check=True)


def planted() -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sstart-"))
    data, reg, scratch, state = d / "projects", d / "registry", d / "scratch", d / "state"
    for p in (data / "known-folder", data / "stray-folder", d / "outside" / "elsewhere", reg, scratch, state, d / "plain"):
        p.mkdir(parents=True)
    for repo in (data / "known-folder", data / "stray-folder", d / "outside" / "elsewhere"):
        git(repo, "init", "-q"); (repo / "f").write_text("x"); git(repo, "add", "f"); git(repo, "commit", "-qm", "seed")
    git(d / "outside" / "elsewhere", "remote", "add", "origin", "git@github.com:Some-Org/elsewhere.git")
    (reg / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:known", "name": "known", "local_folders": ["known-folder"], "last_activity_on": "2026-09-13"}]}), encoding="utf-8")
    (reg / "repositories.json").write_text(json.dumps({"repositories": []}), encoding="utf-8")
    (reg / "relations.json").write_text(json.dumps({"relations": []}), encoding="utf-8")
    (reg / "findings.json").write_text(json.dumps({"findings": [
        {"subject": "project:known", "severity": "critical", "type": "x"},
        {"subject": "secret:known-folder/prod/API", "severity": "warning", "type": "y"},
        {"subject": "project:other", "severity": "critical", "type": "z"}]}), encoding="utf-8")
    (reg / "credentials.json").write_text(json.dumps({"credentials": [
        {"id": "credential:vault/known-folder/prod/API", "used_by": ["project:known"]},
        {"id": "credential:vault/x/prod/B", "used_by": ["project:other"]}]}), encoding="utf-8")
    (reg / "env-inventory.json").write_text(json.dumps({"files": [
        {"path": "known-folder/.env", "project": "known-folder",
         "variables": [{"name": "DB_URL", "class": "secret"}, {"name": "PORT", "class": "config"}]}]}), encoding="utf-8")
    env = {**os.environ, "OBSERVATORY_REGISTRY": str(reg), "OBSERVATORY_DATA": str(data),
           "OBSERVATORY_SCRATCH": str(scratch), "OBSERVATORY_STATE": str(state)}
    return d, env


def run(env: dict, cwd: pathlib.Path, session: str = "s1") -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "tools/session_start.py")], input=json.dumps({"cwd": str(cwd), "session_id": session}),
                          env=env, capture_output=True, text=True, timeout=120)


def test_a_known_project_gets_one_line_with_its_counts() -> None:
    d, env = planted()
    p = run(env, d / "projects" / "known-folder")
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    check("exit 0 and exactly one line", p.returncode == 0 and len(lines) == 1, p.stdout + p.stderr)
    line = lines[0] if lines else ""
    check("the line names the project", line.startswith("observatory: known"), line)
    check("counts only this project's findings — by id and by folder, not the other project's",
          "1 critical / 1 warning" in line, line)
    check("counts keys by name from the registry AND the project's .env, secrets only",
          "keys by name: 2 (registry 1, .env 1)" in line, line)
    check("and hands over the verb and the address", 'use_secret.py" names known' in line and "#project:known" in line, line)
    # RUNNABLE AS PRINTED. A bare `use_secret.py names X` resolves nowhere for an
    # installed user; every other surface hands over the installed path.
    check("the verb is runnable as printed: the tool by its installed path",
          'python "$(project-observatory full-path)/tools/use_secret.py" names known' in line, line)


def test_an_unknown_folder_inside_the_estate_is_said_and_remembered() -> None:
    d, env = planted()
    p = run(env, d / "projects" / "stray-folder", "s-stray")
    check("one line, exit 0", p.returncode == 0 and len(p.stdout.strip().splitlines()) == 1, p.stdout)
    check("it says the folder is not in the registry and names the tick and the command",
          "not in the registry" in p.stdout and "next tick" in p.stdout
          and "project-observatory full local" in p.stdout, p.stdout)
    seen = pathlib.Path(env["OBSERVATORY_SCRATCH"]) / "sessions-seen.jsonl"
    rows = [json.loads(l) for l in seen.read_text(encoding="utf-8").splitlines() if l.strip()] if seen.is_file() else []
    check("and the sighting is a row: cwd, remote, session", len(rows) == 1 and rows[0]["cwd"].endswith("stray-folder")
          and rows[0]["session_id"] == "s-stray", str(rows))


def test_a_checkout_outside_the_estate_is_named_as_unobserved() -> None:
    d, env = planted()
    p = run(env, d / "outside" / "elsewhere")
    check("outside the projects folder: one line that says so, with the remote recorded",
          p.returncode == 0 and len(p.stdout.strip().splitlines()) == 1
          and "outside the configured projects folder" in p.stdout
          and "does not scan it" in p.stdout, p.stdout)
    seen = pathlib.Path(env["OBSERVATORY_SCRATCH"]) / "sessions-seen.jsonl"
    rows = [json.loads(l) for l in seen.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("the remote is kept as owner/name, never the full URL",
          rows and rows[0]["remote"] == "Some-Org/elsewhere", str(rows))


def test_silence_where_it_is_not_its_business() -> None:
    d, env = planted()
    p = run(env, d / "plain")
    check("a directory that is not a repository: nothing, exit 0", p.returncode == 0 and p.stdout == "", repr(p.stdout))
    p = run(env, ROOT)
    check("the observatory itself: nothing", p.returncode == 0 and p.stdout == "", repr(p.stdout))
    p = run(env, d / "nowhere")
    check("a directory that does not exist: nothing", p.returncode == 0 and p.stdout == "", repr(p.stdout))
    broken = {**env, "OBSERVATORY_REGISTRY": str(d / "no-such-registry")}
    p = run(broken, d / "projects" / "known-folder")
    check("an absent registry never raises — the folder is simply unknown", p.returncode == 0 and "observatory:" in p.stdout, p.stdout + p.stderr)


def test_the_sightings_come_back_as_one_board_row_minus_what_a_tick_has_joined() -> None:
    """`project.seen_unobserved`: folders agents opened that the
    registry never joined — one row, worst-first, and a folder the tick has
    since joined is not reported."""
    import importlib.util, datetime
    spec = importlib.util.spec_from_file_location("session_findings", ROOT / "tools/session_findings.py")
    sf = importlib.util.module_from_spec(spec); spec.loader.exec_module(sf)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-seen-"))
    data = d / "projects"; (data / "joined-since").mkdir(parents=True); (data / "still-stray").mkdir()
    (d / "elsewhere").mkdir()
    now = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=datetime.timezone.utc)
    at = lambda days: (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731
    seen = d / "sessions-seen.jsonl"
    rows = [{"at": at(1), "cwd": str(data / "joined-since"), "remote": None, "session_id": "a"},
            {"at": at(2), "cwd": str(data / "still-stray"), "remote": None, "session_id": "b"},
            {"at": at(3), "cwd": str(data / "still-stray"), "remote": None, "session_id": "c"},
            {"at": at(1), "cwd": str(d / "elsewhere"), "remote": "Org/elsewhere", "session_id": "e"},
            {"at": at(40), "cwd": str(d / "ancient"), "remote": None, "session_id": "z"}]
    seen.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n", encoding="utf-8")
    out = sf.findings(seen, {"joined-since"}, data, now)
    check("one info row", len(out) == 1 and out[0]["severity"] == "info" and out[0]["type"] == "project.seen_unobserved", str(out))
    check("it counts the two folders still unknown — not the one a tick joined, not the one older than the window",
          out[0]["title"].startswith("2 folders ") and out[0]["title_args"]["n"] == 2, out[0]["title"])
    check("worst-first: the folder two sessions opened comes before the one opened once, with its remote as owner/name",
          out[0]["detail"].index("still-stray (2 sess.)") < out[0]["detail"].index("elsewhere (1 sess., Org/elsewhere)"),
          out[0]["detail"][:200])
    check("no sightings, no row", sf.findings(d / "missing.jsonl", set(), data, now) == [])
    check("every sighting joined since: no row", sf.findings(seen, {"joined-since", "still-stray"}, data, now)[0]["title"].startswith("1 folder "))
    src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("the board reads the sightings through session_findings", "import session_findings" in src and "sessions-seen.jsonl" in src)


def test_the_hook_is_declared_and_its_doctrine_rewritten() -> None:
    hooks = json.loads((ROOT / "skill/plugins/observatory-log/hooks/hooks.json").read_text(encoding="utf-8"))
    check("hooks.json declares SessionStart beside Stop", "SessionStart" in hooks.get("hooks", {}) and "Stop" in hooks["hooks"])
    description = hooks.get("description", "")
    check("and its description says SessionStart speaks only when it is actionable and never blocks a turn",
          "SessionStart" in description and "actionable" in description
          and "do not block a turn" in description, description[:200])
    sh = (ROOT / "skill/plugins/observatory-log/hooks/session-start.sh").read_text(encoding="utf-8")
    check("the shell hook finds the checkout like record-turn.sh and exits 0 on every path",
          "tools/session_start.py" in sh and "exit 0" in sh and "|| true" in sh)


if __name__ == "__main__":
    print("the line a session opens with — one, actionable, never a value\n")
    for fn in (test_a_known_project_gets_one_line_with_its_counts,
               test_an_unknown_folder_inside_the_estate_is_said_and_remembered,
               test_a_checkout_outside_the_estate_is_named_as_unobserved,
               test_silence_where_it_is_not_its_business,
               test_the_sightings_come_back_as_one_board_row_minus_what_a_tick_has_joined,
               test_the_hook_is_declared_and_its_doctrine_rewritten):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe harness speaks first, in one line, and only when it has something to say\033[0m")
