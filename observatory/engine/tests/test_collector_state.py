#!/usr/bin/env python3
"""Two collectors that could not say what they had already done, or why they failed.

`collectors/compute_deltas.py` and `collectors/scan_events.py` are the two ends
of the agent's input: one measures the registry, the other the machine, and the
agent runs only if the first produces rows. Neither had been audited.

**`diff` decided what to compare from timestamps alone.** `latest_two()` returned
the two newest fingerprints, full stop, so the command had no memory of what it
had compared. Measured:

* re-running `diff` over the same pair wrote every delta AGAIN — 2 rows became 4
  in a fixture, and duplicated rows sat in a real store, all still pending, so
  the agent would have read one change twice;
* a SKIPPED `diff` lost a generation outright. Three snapshots, one diff: the
  rename in the middle generation produced nothing at all, under a printed
  "2 delta(s) written". One failed tick is enough to cause it.

A cursor — `cursors.deltas.diffed_through` — replaces "the two newest" with "the
last state I actually reported". Folding intermediate generations loses nothing:
a delta answers *what moved since we last looked*. It is REPORTED, because a
fold and a quiet skip look identical otherwise.

**`scan_events` could not tell a fact about the subject from a fact about the
run, and one slow checkout killed it.** `git()` returned `""` on any non-zero
exit and caught neither `TimeoutExpired` nor `FileNotFoundError`, so a single
unreadable repository aborted the collector — every repository after it
unscanned, and a `scans` row left with `finished_at` NULL for ever in the one
table retention never prunes. Meanwhile "git ran and there is nothing in the
window" was reported as a degradation, alongside the estate's own POLICY
exclusions: a real scan said `degraded: 11` of which 7 were policy, 4 were
answers, and **none was a fault**. A real fault would have been the twelfth line
in a list nobody reads closely. After: `degraded: 0`.
"""
from __future__ import annotations
import json, os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

# A private workspace of this suite's own, so the collectors read the shipped
# default configuration (retention, exclusions) rather than a machine's; each
# test then redirects the registry, scratch and store to its own copies.
for _name in ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH"):
    os.environ.pop(_name, None)
os.environ["OBSERVATORY_HOME"] = str(pathlib.Path(tmpdir.mkdtemp(prefix="observatory-collector-home-")).resolve() / "home")
subprocess.run([PY, str(ROOT / "observatory.py"), "init"], cwd=ROOT,
               capture_output=True, timeout=120, check=True)
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def workspace() -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-collector-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    return d, dict(os.environ, OBSERVATORY_DB=str(d / "observatory.db"),
                   OBSERVATORY_REGISTRY=str(d / "registry"),
                   OBSERVATORY_SCRATCH=str(d / "scratch"))


def registry(d: pathlib.Path, projects: list, repositories: list = (),
             relations: list = ()) -> None:
    (d / "registry/projects.json").write_text(json.dumps({"projects": projects}))
    (d / "registry/repositories.json").write_text(
        json.dumps({"repositories": list(repositories)}))
    (d / "registry/relations.json").write_text(json.dumps({"relations": list(relations)}))


def deltas(d: pathlib.Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "collectors/compute_deltas.py", *args], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=300)


def rows(d: pathlib.Path, sql: str) -> list:
    """`[]` when the store does not exist or has no such table — which is a
    STRONGER form of "no row" and is exactly what a collector that refused
    before connecting leaves behind."""
    import sqlite3
    f = d / "observatory.db"
    if not f.is_file():
        return []
    conn = sqlite3.connect(f)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ─────────── the diff remembers what it compared ───────────────────────

def test_a_repeated_diff_writes_nothing_twice() -> None:
    d, env = workspace()
    registry(d, [{"id": "project:x", "name": "one", "lifecycle": "active"}])
    deltas(d, env, "snapshot")
    registry(d, [{"id": "project:x", "name": "two", "lifecycle": "active"}])
    deltas(d, env, "snapshot")
    first = deltas(d, env, "diff")
    n1 = rows(d, "SELECT count(*) FROM deltas")[0][0]
    check("the first diff writes the rename", n1 >= 1, first.stdout[-200:])
    second = deltas(d, env, "diff")
    n2 = rows(d, "SELECT count(*) FROM deltas")[0][0]
    check("the second writes nothing at all", n2 == n1, f"{n1} -> {n2}")
    check("and says why rather than printing a fresh count",
          "already diffed" in second.stdout, second.stdout[-200:])
    cur = rows(d, "SELECT name, value FROM cursors")
    check("the cursor records where it got to",
          any(r[0] == "deltas.diffed_through" for r in cur), str(cur))


def test_a_skipped_diff_folds_the_generation_instead_of_losing_it() -> None:
    d, env = workspace()
    for name, stack in (("first", ["python"]), ("second", ["python"]), ("third", ["rust"])):
        registry(d, [{"id": "project:x", "name": name, "lifecycle": "active",
                      "stack": stack}])
        deltas(d, env, "snapshot")
    p = deltas(d, env, "diff")
    got = [(r[0], r[1], r[2]) for r in
           rows(d, "SELECT kind, before_json, after_json FROM deltas")]
    check("the rename from the FIRST generation is present",
          any('"first"' in (r[1] or "") for r in got), str(got))
    check("as one delta spanning the fold, not two",
          len([r for r in got if r[0] == "name-changed"]) == 1, str(got))
    check("the stack change is there too",
          any(r[0] == "stack-changed" for r in got), str(got))
    check("and the fold is REPORTED", "folded" in p.stdout or "folding" in p.stdout,
          p.stdout[-260:])


def test_a_pruned_cursor_target_degrades_out_loud() -> None:
    """Retention keeps a few fingerprints. When the one the cursor names is
    gone, changes before the oldest survivor CANNOT be recovered — and that is
    the one case where a delta is genuinely lost, so it must be said."""
    d, env = workspace()
    for name in ("a", "b", "c"):
        registry(d, [{"id": "project:x", "name": name, "lifecycle": "active"}])
        deltas(d, env, "snapshot")
    import sqlite3
    conn = sqlite3.connect(d / "observatory.db")
    with conn:
        conn.execute("INSERT INTO cursors (name, value, updated_at)"
                     " VALUES ('deltas.diffed_through','fingerprint-pruned-away','x')")
    conn.close()
    p = deltas(d, env, "diff")
    check("the loss is named", "pruned" in p.stdout and "not recoverable" in p.stdout,
          p.stdout[-300:])
    check("and it still writes what it CAN measure",
          rows(d, "SELECT count(*) FROM deltas")[0][0] >= 1, p.stdout[-200:])


def test_pending_names_its_cap() -> None:
    d, env = workspace()
    registry(d, [{"id": f"project:p{i}", "name": f"p{i}", "lifecycle": "active"}
                 for i in range(70)])
    deltas(d, env, "snapshot")
    registry(d, [{"id": f"project:p{i}", "name": f"renamed{i}", "lifecycle": "active"}
                 for i in range(70)])
    deltas(d, env, "snapshot")
    deltas(d, env, "diff")
    p = deltas(d, env, "pending")
    total = rows(d, "SELECT count(*) FROM deltas WHERE consumed_at IS NULL")[0][0]
    check("there are more pending than the cap shows", total > 60, str(total))
    check("the output states the total, not only the page",
          f"of {total} unconsumed" in p.stdout, p.stdout.splitlines()[-1] if p.stdout else "")
    check("and names what it left out", "not listed" in p.stdout,
          p.stdout.splitlines()[-1] if p.stdout else "")


def test_an_unreadable_registry_is_not_fingerprinted_as_empty() -> None:
    """`{}` here would make every project look disappeared, and the next diff
    would hand the agent one `project-disappeared` per project."""
    d, env = workspace()
    registry(d, [{"id": "project:x", "name": "one", "lifecycle": "active"}])
    deltas(d, env, "snapshot")
    (d / "registry/projects.json").write_text('{"projects": [{"id": "x",')  # truncated
    p = deltas(d, env, "snapshot")
    check("the snapshot refuses", p.returncode == 1, f"exit {p.returncode}")
    # PORTED-DIVERGED: the engine validates every registry document when
    # `paths` is imported (a compatibility gate that also refuses future
    # formats), so an unreadable document is refused before the collector's
    # own message can print. The refusal still names the document.
    check("and says which document and why",
          "projects.json" in p.stderr and "Cannot read" in p.stderr,
          p.stderr[-200:])
    import sqlite3
    conn = sqlite3.connect(d / "observatory.db")
    n = conn.execute("SELECT count(*) FROM observations WHERE kind = 'project-fingerprints'"
                     ).fetchone()[0]
    conn.close()
    check("no second fingerprint was written", n == 1, f"{n} fingerprint(s)")


# ─────────── scan_events: three outcomes, and no crash ─────────────────

def test_a_stalled_diff_becomes_a_finding() -> None:
    """The cursor's second reader, and the reason it is not exempt from the
    dead-data rule.

    `snapshot` and `diff` are two steps. The first can keep writing fingerprints
    while the second stops — a raise, a lock, a step dropped from the tick — and
    the only visible effect is an agent with nothing to do, which is exactly what
    a quiet estate looks like. Planted here by ageing the cursor.
    """
    d, env = workspace()
    registry(d, [{"id": "project:x", "name": "one", "lifecycle": "active"}])
    deltas(d, env, "snapshot")
    registry(d, [{"id": "project:x", "name": "two", "lifecycle": "active"}])
    deltas(d, env, "snapshot")
    deltas(d, env, "diff")

    def findings() -> list[dict]:
        subprocess.run([PY, "tools/build_findings.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
        f = d / "registry/findings.json"
        doc = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        return [x for x in doc.get("findings", []) if x["type"] == "deltas.not_diffed"]

    check("a current diff raises nothing", findings() == [], "the quiet case first")

    import sqlite3
    conn = sqlite3.connect(d / "observatory.db")
    with conn:
        # A newer fingerprint the diff never reached, and a cursor that has not
        # advanced for a day.
        conn.execute("INSERT INTO scans (id, started_at, collector_version)"
                     " VALUES ('fingerprint-stalled','2026-09-07T00:00:00Z','t')")
        conn.execute("INSERT INTO observations (id, scan_id, subject_id, kind,"
                     " payload_json, observed_at) VALUES"
                     " ('obs:stalled','fingerprint-stalled','estate',"
                     " 'project-fingerprints','{}','2099-01-01T00:00:00Z')")
        conn.execute("UPDATE cursors SET updated_at = '2026-01-01T00:00:00Z'"
                     " WHERE name = 'deltas.diffed_through'")
    conn.close()
    got = findings()
    check("a stalled diff IS a finding", len(got) == 1, str(got)[:200])
    if got:
        check("it says how long", "has not been diffed" in got[0]["title"], got[0]["title"])
        check("and names the queue the agent is being handed",
              "empty queue" in got[0]["detail"], got[0]["detail"][:160])
        check("with the cursor as its evidence",
              any("diffed_through" in e for e in got[0]["evidence"]),
              str(got[0]["evidence"]))


def test_git_returns_a_reason_for_each_way_it_can_fail() -> None:
    import importlib
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_events as SE
    importlib.reload(SE)
    import subprocess as sp

    real = sp.run
    for exc, expect in ((FileNotFoundError(), "not installed"),
                        (sp.TimeoutExpired(cmd="git", timeout=60), "did not answer"),
                        (OSError("boom"), "could not be run")):
        sp.run = lambda *a, **k: (_ for _ in ()).throw(exc)
        try:
            out, reason = SE.git(pathlib.Path("/nowhere"), "log")
        finally:
            sp.run = real
        check(f"{type(exc).__name__} becomes a reason, not a traceback",
              out == "" and reason is not None and expect in reason, str(reason))

    class Bad:
        returncode, stdout, stderr = 128, "", "fatal: not a git repository"
    sp.run = lambda *a, **k: Bad()
    try:
        out, reason = SE.git(pathlib.Path("/nowhere"), "log")
    finally:
        sp.run = real
    check("a non-zero exit carries git's own message",
          "128" in (reason or "") and "not a git repository" in (reason or ""), str(reason))


def test_a_quiet_repository_is_an_answer_and_a_broken_one_is_not() -> None:
    """Built as two real checkouts: one with a commit inside the window, one
    whose only commit is two years old. The second is QUIET — git answered — and
    the third entry has no `.git` at all, which is a fault."""
    d, env = workspace()
    def repo(name: str, when: str | None) -> pathlib.Path:
        r = d / name
        r.mkdir()
        run = lambda *a, **kw: subprocess.run(["git", "-C", str(r), *a], check=True,
                                              capture_output=True, **kw)
        subprocess.run(["git", "init", "-q", str(r)], check=True, capture_output=True)
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "T")
        (r / "f.txt").write_text("x")
        run("add", "f.txt")
        e = dict(os.environ)
        if when:
            e["GIT_COMMITTER_DATE"] = e["GIT_AUTHOR_DATE"] = when
        run("commit", "-q", "-m", f"commit in {name}", env=e)
        return r
    fresh = repo("fresh", None)
    stale = repo("stale", "2023-01-01T00:00:00Z")
    registry(
        d,
        projects=[{"id": "project:x", "name": "x", "lifecycle": "active",
                   "ownership": "owned"}],
        repositories=[
            {"id": "repo:gh:o/fresh", "name_with_owner": "o/fresh",
             "local": {"path": str(fresh)}},
            {"id": "repo:gh:o/stale", "name_with_owner": "o/stale",
             "local": {"path": str(stale)}},
            {"id": "repo:gh:o/gone", "name_with_owner": "o/gone",
             "local": {"path": str(d / "does-not-exist")}},
        ],
        relations=[{"type": "implemented_by", "from": "project:x", "to": r}
                   for r in ("repo:gh:o/fresh", "repo:gh:o/stale", "repo:gh:o/gone")])
    p = subprocess.run([PY, "collectors/scan_events.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    check("the scan succeeds", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    counts = json.loads(rows(d, "SELECT counts_json FROM scans WHERE collector_version"
                                " LIKE 'scan_events%' ORDER BY rowid DESC LIMIT 1")[0][0])
    check("the stale checkout is QUIET, not degraded", counts.get("repos_quiet") == 1,
          json.dumps(counts))
    deg = json.loads(rows(d, "SELECT degraded_json FROM scans WHERE collector_version"
                             " LIKE 'scan_events%' ORDER BY rowid DESC LIMIT 1")[0][0])
    check("the missing checkout IS degraded, alone", len(deg) == 1, json.dumps(deg))
    check("and names the path it looked for", "no .git" in json.dumps(deg), json.dumps(deg))
    check("the fresh commit was recorded",
          rows(d, "SELECT count(*) FROM events WHERE kind='commit'")[0][0] == 1)
    check("the scan row is closed", rows(
        d, "SELECT count(*) FROM scans WHERE finished_at IS NULL")[0][0] == 0)


def test_a_policy_exclusion_is_not_a_degradation() -> None:
    d, env = workspace()
    registry(
        d,
        projects=[{"id": "project:e", "name": "e", "lifecycle": "active",
                   "ownership": "external"}],
        repositories=[{"id": "repo:gh:o/e", "name_with_owner": "o/e",
                       "local": {"path": str(d / "nope")}}],
        relations=[{"type": "implemented_by", "from": "project:e", "to": "repo:gh:o/e"}])
    p = subprocess.run([PY, "collectors/scan_events.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    deg = json.loads(rows(d, "SELECT degraded_json FROM scans ORDER BY rowid DESC LIMIT 1")[0][0])
    counts = json.loads(rows(d, "SELECT counts_json FROM scans ORDER BY rowid DESC LIMIT 1")[0][0])
    check("an external repository is excluded", counts.get("repos_excluded") == 1,
          json.dumps(counts))
    check("and is NOT in degraded", deg == [], json.dumps(deg))
    check("the report groups exclusions by rule", "external" in p.stdout, p.stdout[-300:])


def test_an_unreadable_registry_writes_no_scan_row() -> None:
    d, env = workspace()
    (d / "registry/projects.json").write_text("{oops")
    p = subprocess.run([PY, "collectors/scan_events.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    check("the collector refuses", p.returncode == 1, f"exit {p.returncode}")
    # PORTED-DIVERGED: refused by the registry gate at import, as above.
    check("and names the unreadable document",
          "projects.json" in p.stderr and "Cannot read" in p.stderr, p.stderr[-200:])
    scans = rows(d, "SELECT count(*) FROM scans")
    check("no scan row was opened", scans == [] or scans[0][0] == 0,
          "a scans row with no measurement behind it is a broken spine segment")


def test_an_abort_inside_the_loop_still_closes_the_scan_row() -> None:
    """The planted defect: `estate.records_events` raises on the second
    repository. Before the wrapper this left `finished_at` NULL for ever in the
    one table retention never prunes."""
    d, env = workspace()
    registry(
        d,
        projects=[{"id": "project:x", "name": "x", "lifecycle": "active",
                   "ownership": "owned"}],
        repositories=[{"id": f"repo:gh:o/r{i}", "name_with_owner": f"o/r{i}",
                       "local": {"path": str(d / f"r{i}")}} for i in range(3)],
        relations=[{"type": "implemented_by", "from": "project:x", "to": f"repo:gh:o/r{i}"}
                   for i in range(3)])
    code = ("import sys; sys.path.insert(0,'.')\n"
            "import estate\n"
            "seen = []\n"
            "real = estate.records_events\n"
            "def boom(own):\n"
            "    seen.append(own)\n"
            "    if len(seen) == 2: raise RuntimeError('planted')\n"
            "    return real(own)\n"
            "estate.records_events = boom\n"
            "import collectors.scan_events as SE\n"
            "raise SystemExit(SE.main())\n")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=600)
    check("the collector exits non-zero", p.returncode == 1, f"exit {p.returncode}")
    check("it says the scan aborted", "SCAN ABORTED" in p.stderr, p.stderr[-260:])
    check("no scan row is left unfinished",
          rows(d, "SELECT count(*) FROM scans WHERE finished_at IS NULL")[0][0] == 0,
          "`scans` is never pruned, so an unfinished row stays broken for ever")
    counts = json.loads(rows(d, "SELECT counts_json FROM scans ORDER BY rowid DESC LIMIT 1")[0][0])
    check("and the row records the abort", counts.get("aborted") is True, json.dumps(counts))
    deg = json.loads(rows(d, "SELECT degraded_json FROM scans ORDER BY rowid DESC LIMIT 1")[0][0])
    check("with the exception as a degradation",
          any("planted" in (x.get("reason") or "") for x in deg), json.dumps(deg))


def test_one_rule_reads_the_registry() -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import check_paths
    for f in ("collectors/scan_events.py", "collectors/compute_deltas.py"):
        src = check_paths.prose_removed((ROOT / f).read_text(encoding="utf-8"))
        check(f"{f} reads the registry through the shared rule",
              "registry_read.read(" in src, "a bare json.loads is a traceback")
        check(f"{f} no longer loads a registry document directly",
              'REGISTRY / "projects.json"' not in src, "two rules, one registry")


if __name__ == "__main__":
    print("the two collectors — what they remember, and what they can say went wrong\n")
    for fn in (test_a_repeated_diff_writes_nothing_twice,
               test_a_skipped_diff_folds_the_generation_instead_of_losing_it,
               test_a_pruned_cursor_target_degrades_out_loud,
               test_pending_names_its_cap,
               test_an_unreadable_registry_is_not_fingerprinted_as_empty,
               test_a_stalled_diff_becomes_a_finding,
               test_git_returns_a_reason_for_each_way_it_can_fail,
               test_a_quiet_repository_is_an_answer_and_a_broken_one_is_not,
               test_a_policy_exclusion_is_not_a_degradation,
               test_an_unreadable_registry_writes_no_scan_row,
               test_an_abort_inside_the_loop_still_closes_the_scan_row,
               test_one_rule_reads_the_registry):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma diff that knows what it diffed, and a scan that can say why it "
          "could not look\033[0m")
