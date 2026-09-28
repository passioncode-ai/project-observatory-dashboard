#!/usr/bin/env python3
"""The commit loop read repositories; its subject was always a checkout.

`collectors/scan_events.py` once iterated `repositories.json` and read history
from each `local["path"]`. A git folder with no remote has no repository row —
there is no `owner/name` to key one by — so **its history was never read**:
hundreds of commits across a few unpublished folders were invisible.

Nothing downstream needed changing to hold them, which is what says the subject
was wrong rather than the schema: `events` carries `project_id` AND `repo_id`,
`repo_id` is nullable and already used NULL, `store/rollup.py`
groups by `project_id` and never mentions `repo_id`, and neither does retention
or the MCP server. The loop was the only place that insisted on a repository.

**And the guard against the two readers disagreeing could not detect it.**
`estate.py` exists because `tools/record_turn.py` refused third-party history
while the collector recorded all of it, and its preamble says "one rule, two
readers". But `record_turn.py` kept its OWN copy of the set, and the test named
`test_one_rule_two_readers` compared `estate.RECORDED_OWNERSHIP` against a
literal spelled out in the test, then merely checked that the STRING
"RECORDED_OWNERSHIP" appeared in the recorder's source. Driven against a
planted copy that read `{"external"}` — the recorder inverted to record
ONLY somebody else's history — **both assertions passed.** The recorder imports
the rule now, and the comparison is between two real objects.

`local-only` joins the recorded set for the reason the set exists: it was drawn
against a stranger's history, and an unpublished folder is the opposite of that —
the operator's own work, existing nowhere else.
"""
from __future__ import annotations
import importlib, json, os, pathlib, sqlite3, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
import tmp as tmpdir                                                # noqa: E402
import estate                                                      # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


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


# ─────────── one rule, and now actually one object ─────────────────────

def test_the_recorder_does_not_keep_its_own_copy_of_the_rule() -> None:
    """The planted defect this replaces: with a local copy, `record_turn.py`
    could be inverted to record ONLY external projects and the old assertions
    both passed. `sys.path` already reaches the root — it imports `atomic` and
    `paths` from there — so the copy was never necessary."""
    src = (ROOT / "tools/record_turn.py").read_text(encoding="utf-8")
    check("the recorder imports the rule", "import estate" in src, "")
    check("and defines no set of its own",
          "RECORDED_OWNERSHIP = {" not in src and "RECORDED_OWNERSHIP = frozenset" not in src,
          "a rule in one place and copied in another is two rules")
    check("it asks the shared function rather than testing membership itself",
          "estate.records_events" in src, "")


def test_the_guard_compares_two_real_objects() -> None:
    """What the old version could not do. `estate` is imported by both, so there
    is one object to compare — and the test asserts identity rather than
    re-spelling the contents, which is what let a divergence hide."""
    import record_turn as R
    importlib.reload(R)
    check("the recorder's rule IS the estate's rule",
          getattr(R, "RECORDED_OWNERSHIP", None) is None
          or R.RECORDED_OWNERSHIP is estate.RECORDED_OWNERSHIP,
          "not a copy that happens to be equal today")
    check("and the recorder's decision follows it",
          R.records_events("external") is False
          and R.records_events("owned") is True
          if hasattr(R, "records_events") else
          estate.records_events("external") is False, "")


def test_local_only_is_the_operators_own_work() -> None:
    check("an unpublished folder records events",
          estate.records_events("local-only"),
          "the rule was drawn against a stranger's history; this is the opposite")
    check("a third-party clone still does not",
          not estate.records_events("external"), "")
    check("and the other two are unchanged",
          estate.records_events("owned") and estate.records_events("work-bitbucket"), "")
    check("a checkout attached to no project still records — the gap stays visible",
          estate.records_events(None), "")
    check("the reason is recorded where the set is",
          "local-only" in (ROOT / "estate.py").read_text(encoding="utf-8"),
          "a value added to a policy set without its reason is a value nobody "
          "can argue with later")


# ─────────── the loop's subject ────────────────────────────────────────

def test_targets_come_from_both_sources() -> None:
    import scan_events as S
    importlib.reload(S)
    repos = {"repository:o/r": {"name_with_owner": "o/r", "created_on": "2020-01-01",
                                "local": {"path": "/x/r"}}}
    projects = [
        {"id": "project:p", "name": "p"},
        {"id": "project:local-solo", "name": "solo",
         "local_only": {"folder": "solo", "path": "/x/solo", "unpublished": True}},
        {"id": "project:local-notgit", "name": "notgit",
         "local_only": {"folder": "notgit", "path": "/x/notgit", "unpublished": False}},
    ]
    got = S.targets(projects, repos, {"repository:o/r": "project:p"})
    by_label = {t["label"]: t for t in got}
    check("a repository is a target", "repository:o/r" in by_label, str(sorted(by_label)))
    check("with its repository id", by_label.get("repository:o/r", {}).get("repo_id")
          == "repository:o/r", "")
    check("an unpublished folder is a target too", "project:local-solo" in by_label,
          str(sorted(by_label)))
    check("with NO repository id, because there is no owner/name to key one by",
          "project:local-solo" in by_label
          and by_label["project:local-solo"]["repo_id"] is None, "")
    check("and its project id is its own",
          by_label.get("project:local-solo", {}).get("project_id")
          == "project:local-solo", "")
    check("a local folder that is not a git repository is not a target",
          "project:local-notgit" not in by_label,
          "`unpublished` is `is_git and no remote` — there is no history to read")
    check("a repository with no checkout here is not a target",
          len(got) == 2, str(sorted(by_label)))


def test_an_unpublished_folder_sorts_last_for_shared_ancestry() -> None:
    """The ordering decides which target owns a commit two of them hold. A
    published repository is the better home for shared history than an
    unpublished folder, so a target with no creation date sorts last — the same
    direction the missing-date default already pointed."""
    import scan_events as S
    importlib.reload(S)
    ts = [{"label": "project:local-x", "created_on": ""},
          {"label": "repository:o/old", "created_on": "2019-01-01"},
          {"label": "repository:o/new", "created_on": "2025-01-01"}]
    order = [t["label"] for t in sorted(ts, key=S.by_age)]
    check("oldest repository first, unpublished folder last",
          order == ["repository:o/old", "repository:o/new", "project:local-x"],
          str(order))


# ─────────── end to end, against a real remoteless repository ──────────

def planted() -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-checkout-"))
    repo = d / "solo"
    repo.mkdir()
    git(["init", "--initial-branch=main"], repo)
    for n in ("a", "b", "c"):
        (repo / n).write_text(n, encoding="utf-8")
        git(["add", n], repo)
        git(["-c", "user.email=t@t", "-c", "user.name=Tester", "commit", "-m", f"add {n}"],
            repo)
    reg = d / "registry"
    reg.mkdir()
    (reg / "projects.json").write_text(json.dumps({"projects": [
        {"id": "project:local-solo", "name": "solo", "ownership": "local-only",
         "local_only": {"folder": "solo", "path": str(repo), "unpublished": True}}]}))
    (reg / "repositories.json").write_text('{"repositories": []}')
    (reg / "relations.json").write_text('{"relations": []}')
    (d / "scratch").mkdir()
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(reg),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_DB=str(d / "observatory.db"))
    return d, env


def run_scan(env: dict) -> subprocess.CompletedProcess:
    subprocess.run([PY, "store/migrate.py"], cwd=ROOT, env=env,
                   capture_output=True, text=True, timeout=600)
    return subprocess.run([PY, "collectors/scan_events.py"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=900)


def test_a_remoteless_checkouts_history_reaches_the_store() -> None:
    d, env = planted()
    p = run_scan(env)
    check("the scan runs", p.returncode == 0, (p.stdout + p.stderr)[-400:])
    con = sqlite3.connect(d / "observatory.db")
    rows = con.execute("SELECT project_id, repo_id, kind, ref FROM events"
                       " WHERE kind = 'commit'").fetchall()
    check("all three commits are recorded", len(rows) == 3,
          f"{len(rows)}: {rows} / {(p.stdout + p.stderr)[-300:]}")
    check("against the project", all(r[0] == "project:local-solo" for r in rows),
          str(rows))
    check("with a NULL repository id rather than an invented one",
          all(r[1] is None for r in rows), str(rows))
    con.close()


def test_the_rollup_counts_them_without_a_repository() -> None:
    """`store/rollup.py` groups by `project_id` and never mentions `repo_id`,
    which is the measurement that said the subject was wrong rather than the
    schema. Driven, so the claim is not just a reading of the source."""
    d, env = planted()
    run_scan(env)
    r = subprocess.run([PY, "store/rollup.py", "refresh"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=900)
    check("the rollup runs", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    con = sqlite3.connect(d / "observatory.db")
    got = con.execute("SELECT project_id, SUM(commits) FROM project_week"
                      " GROUP BY project_id").fetchall()
    con.close()
    check("the unpublished project has a week row",
          any(row[0] == "project:local-solo" and row[1] == 3 for row in got), str(got))


def test_the_new_source_is_counted_rather_than_folded_in() -> None:
    """A count that folds a new source into an old total reads as "nothing
    changed" — the silent-cap shape. The receipt names how many checkouts had no
    repository."""
    d, env = planted()
    p = run_scan(env)
    con = sqlite3.connect(d / "observatory.db")
    row = con.execute("SELECT counts_json FROM scans ORDER BY started_at DESC"
                      " LIMIT 1").fetchone()
    con.close()
    counts = json.loads(row[0]) if row and row[0] else {}
    check("the scan row records the unpublished checkouts separately",
          counts.get("checkouts_unpublished") == 1, json.dumps(counts))
    check("and the stdout says so", "unpublished" in p.stdout.lower(),
          p.stdout[-300:])


def test_an_external_project_is_still_refused() -> None:
    """The policy still applies to the new source: an unpublished folder whose
    project is somehow `external` records nothing."""
    d, env = planted()
    reg = pathlib.Path(env["OBSERVATORY_REGISTRY"])
    doc = json.loads((reg / "projects.json").read_text(encoding="utf-8"))
    doc["projects"][0]["ownership"] = "external"
    (reg / "projects.json").write_text(json.dumps(doc))
    p = run_scan(env)
    con = sqlite3.connect(d / "observatory.db")
    n = con.execute("SELECT count(*) FROM events WHERE kind = 'commit'").fetchone()[0]
    con.close()
    check("nothing is recorded", n == 0, f"{n} commit(s) recorded / {p.stdout[-200:]}")
    check("and the exclusion is reported by rule", "excluded 1" in p.stdout,
          p.stdout[-300:])


def test_an_unreadable_checkout_is_a_fault_not_a_quiet_project() -> None:
    """The reporting the refactor had to preserve: `quiet` means git answered
    with nothing, `unreadable` means it could not answer. Conflating them is how
    a broken checkout reads as an idle project."""
    d, env = planted()
    reg = pathlib.Path(env["OBSERVATORY_REGISTRY"])
    doc = json.loads((reg / "projects.json").read_text(encoding="utf-8"))
    doc["projects"][0]["local_only"]["path"] = str(d / "not-there")
    (reg / "projects.json").write_text(json.dumps(doc))
    p = run_scan(env)
    check("the scan still finishes", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    con = sqlite3.connect(d / "observatory.db")
    row = con.execute("SELECT degraded_json FROM scans ORDER BY started_at DESC"
                      " LIMIT 1").fetchone()
    con.close()
    deg = json.loads(row[0]) if row and row[0] else []
    check("the missing checkout is named as a degradation",
          any("project:local-solo" in json.dumps(x) for x in deg), json.dumps(deg)[:300])


if __name__ == "__main__":
    print("the loop's subject — a checkout, not a repository\n")
    for fn in (test_the_recorder_does_not_keep_its_own_copy_of_the_rule,
               test_the_guard_compares_two_real_objects,
               test_local_only_is_the_operators_own_work,
               test_targets_come_from_both_sources,
               test_an_unpublished_folder_sorts_last_for_shared_ancestry,
               test_a_remoteless_checkouts_history_reaches_the_store,
               test_the_rollup_counts_them_without_a_repository,
               test_the_new_source_is_counted_rather_than_folded_in,
               test_an_external_project_is_still_refused,
               test_an_unreadable_checkout_is_a_fault_not_a_quiet_project):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery checkout with history is read, whatever keys it\033[0m")
