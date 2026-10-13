#!/usr/bin/env python3
"""The second witness: what it refuses, and what a refusal is worth.

`tools/corroborate.py` is the only path that promotes a ledger row without a
person, so its refusals matter more than its promotions. Where it was first
measured it had promoted a handful of `session` rows and refused nothing — so the
four refusal paths had never been watched, and by this repository's own rule a
green from a check nobody has watched fail is not evidence.

Each refusal is driven here against a REAL git repository, including a genuinely
rewritten history (`reset --hard` + `reflog expire` + `gc --prune=now`), because
`git cat-file -e` still finds an unreachable object until it is collected — so a
fixture that only moves a branch would test the wrong branch of the code.

**And a refusal is a FINDING**, which this tool's own docstring says: *"If the
sha is GONE — force-push, branch deleted, clone rebuilt — the row is NOT
promoted. That is a finding, not a corroboration."* It went to stdout, which the
tick swallows into a log; the row then waits `proposed` until retention erases it
at ninety days, taking the only record of the vanished work with it.

What was NOT wrong, recorded so it is not "fixed" later: the check verifies the
sha both EXISTS and is an ancestor of the recorded branch. Existence alone would
have been the weak version — an unreferenced object survives a force-push — and
the code does the strong one.
"""
from __future__ import annotations
import importlib, importlib.util, json, os, pathlib, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# The board and the ledger read a workspace; the synthetic estate is it.
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def git(cwd: pathlib.Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def repo_with_history() -> tuple[pathlib.Path, str, str]:
    """A real repository: two commits on master, and a branch left behind."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-corr-repo-"))
    git(d, "init", "-q", "-b", "master")
    git(d, "config", "user.email", "fixture@example.invalid")
    git(d, "config", "user.name", "Fixture")
    (d / "a.txt").write_text("one\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "first")
    first = git(d, "rev-parse", "HEAD")
    git(d, "branch", "left-behind")          # points at `first`
    (d / "a.txt").write_text("two\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "second")
    second = git(d, "rev-parse", "HEAD")
    return d, first, second


def sandbox(repo: pathlib.Path | None, nwo: str = "fixture/repo"):
    """A registry naming that clone, so `clone_path` can resolve it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-corr-"))
    (d / "registry").mkdir()
    local = {"path": str(repo)} if repo else None
    (d / "registry/repositories.json").write_text(json.dumps({
        "schema_version": 2,
        "repositories": [{"id": f"repository:{nwo}", "name_with_owner": nwo,
                          "host": "github", "local": local}]}), encoding="utf-8")
    os.environ["OBSERVATORY_REGISTRY"] = str(d / "registry")
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    return d


def load_corroborator():
    spec = importlib.util.spec_from_file_location("corr_t", ROOT / "tools/corroborate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["corr_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def row(evidence: list[dict], *, owner: str = "agent:fixture",
        state: str = "proposed", kind: str = "session") -> dict:
    """A ledger row shaped like the real thing, as a plain mapping."""
    return {"memory_id": "mem:fixture", "revision": 1, "kind": kind, "owner": owner,
            "state": state, "evidence_json": json.dumps(evidence)}


# ─────────────────────── the four refusals, driven ──────────────────────

def test_a_row_with_no_recorded_head_is_refused() -> None:
    repo, first, second = repo_with_history()
    sandbox(repo)
    m = load_corroborator()
    ok, why = m.check_session(row([{"uri": "repo:repository:fixture/repo",
                                    "branch": "master"}]))
    check("no head recorded is a refusal", not ok, why)
    check("and the reason says so", "no repository head" in why, why)


def test_a_missing_clone_is_refused() -> None:
    sandbox(None)
    m = load_corroborator()
    ok, why = m.check_session(row([{"uri": "repo:repository:fixture/repo",
                                    "head": "deadbeef", "branch": "master"}]))
    check("a repository with no local clone is a refusal", not ok, why)
    check("and the reason names the repository", "fixture/repo" in why, why)


def test_a_rewritten_history_is_refused() -> None:
    """Genuinely gone, not merely unreferenced.

    `git cat-file -e` finds an unreachable object until it is collected, so this
    does what a force-push plus housekeeping does: reset, expire the reflog, and
    collect. A fixture that only moved the branch would exercise the ancestor
    check instead — a different refusal with a different message.
    """
    repo, first, second = repo_with_history()
    git(repo, "reset", "--hard", first)
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "--prune=now", "-q")
    gone = subprocess.run(["git", "cat-file", "-e", f"{second}^{{commit}}"], cwd=repo,
                          capture_output=True).returncode != 0
    check("the fixture really removed the object", gone,
          "gc kept it, so the next check would test the wrong branch")
    sandbox(repo)
    m = load_corroborator()
    ok, why = m.check_session(row([{"uri": "repo:repository:fixture/repo",
                                    "head": second, "branch": "master"}]))
    check("a sha that is no longer an object is a refusal", not ok, why)
    check("and the reason says the work was rewritten",
          "rewritten or the clone was rebuilt" in why, why)


def test_a_sha_that_fell_off_its_branch_is_refused() -> None:
    """It exists, and the branch moved away from it — the weaker failure that
    an existence-only check would have passed."""
    repo, first, second = repo_with_history()
    sandbox(repo)
    m = load_corroborator()
    ok, why = m.check_session(row([{"uri": "repo:repository:fixture/repo",
                                    "head": second, "branch": "left-behind"}]))
    check("a sha outside the recorded branch is a refusal", not ok, why)
    check("and the reason distinguishes it from a missing object",
          "is not an ancestor of" in why, why)

    # The control: the same sha on the branch that does contain it passes.
    ok2, why2 = m.check_session(row([{"uri": "repo:repository:fixture/repo",
                                      "head": second, "branch": "master"}]))
    check("while the branch that holds it corroborates", ok2, why2)
    check("and the promotion states what git was asked",
          "still an ancestor of" in why2, why2)


def test_the_check_verifies_BOTH_existence_and_ancestry() -> None:
    """Recorded because it was suspected and found sound: existence alone is the
    weak version, since an unreferenced object survives a force-push."""
    # THROUGH `code_keeping_strings`, and the choice of reader is the point.
    # Reading the RAW source made this compare PROSE against code: a docstring
    # explaining `merge-base --is-ancestor` now sits above the `cat-file` call,
    # so the order test failed while the code was in the right order — the
    # eighth instance of matching what the code says instead of
    # what it does. But `code_only` blanks string literals too, and the things
    # being asserted here ARE literals: `cat-file` and `--is-ancestor` are git's
    # own subcommands, passed as arguments. Prose removed, literals kept is
    # exactly the third reader's job.
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_keeping_strings(
        (ROOT / "tools/corroborate.py").read_text(encoding="utf-8"))
    check("it asks whether the object exists", "cat-file" in code)
    check("and whether it is an ancestor of the recorded branch",
          "merge-base" in code and "--is-ancestor" in code)
    check("in that order, so the messages can differ",
          code.index("cat-file") < code.index("--is-ancestor"))


# ─────────────────────── one gate, and it is positive ───────────────────

def test_only_session_rows_are_eligible_and_the_rule_is_singular() -> None:
    src = (ROOT / "tools/corroborate.py").read_text(encoding="utf-8")
    check("the gate is the positive rule", 'row["kind"] != "session"' in src)
    # In the CODE, not in the file: the words survive in the comment explaining
    # the removal. This was hand-rolled here first and is now the shared
    # `tests/source_reader.py`, after the same shape broke five assertions.
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    lines = source_reader.code_lines(src, "NO_MECHANICAL_CHECK")
    check("and the unreachable denylist above it is gone", not lines,
          f"still at line(s) {lines} — a denylist above the real gate is taken "
          f"for the gate")


def test_the_ledger_still_refuses_a_self_witness() -> None:
    """The guard that matters most, at the layer that owns it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-corr-ledger-"))
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    from store import ledger as L
    importlib.reload(L)
    conn = sdb.connect()
    r = L.append(conn, owner="agent:corroborator", statement="I saw a session",
                 kind="session", state="proposed", confidence=0.5)
    for by, expect in (("agent:corroborator", "a second witness cannot be the first"),
                       ("operator", "approves through review")):
        try:
            L.corroborate(conn, r["memoryId"], by=by, check={"how": "x"},
                          expected_revision=r["revision"])
            check(f"{by} is refused", False, "the promotion succeeded")
        except L.LedgerError as exc:
            check(f"{by} is refused", expect in str(exc), str(exc)[:120])
    conn.close()


# ─────────────────────── a refusal reaches a person ─────────────────────

def test_a_refusal_becomes_a_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-corr-find-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/corroboration.json").write_text(json.dumps({
        "checked_on": "2026-09-07",
        "counts": {"proposed": 1, "promoted": 0, "refused": 1, "needs_person": 0},
        "refused": [{"memory_id": "mem:vanished",
                     "why": "abc1234 is no longer an object in fixture/repo — the work "
                            "was rewritten or the clone was rebuilt"}]}), encoding="utf-8")
    # OBSERVATORY_SCRATCH alone, not the whole state directory: one variable
    # per artefact is the pattern `DB` already follows, and the builder's
    # configuration stays in the workspace it was initialized with.
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "scratch"),
               OBSERVATORY_DB=str(d / "observatory.db"))
    p = subprocess.run([PY, "tools/build_findings.py", "--json"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    try:
        findings = json.loads(p.stdout)["findings"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    rows = [f for f in findings if f["type"] == "work.unverifiable"]
    check("a refusal is raised as a finding", bool(rows),
          "the tool's own docstring calls it one")
    if rows:
        f = rows[0]
        check("against the row it refused", f["subject"] == "mem:vanished", f["subject"])
        check("as a warning", f["severity"] == "warning", f["severity"])
        check("carrying the reason git gave", "rewritten" in f["detail"], f["detail"][:120])
        check("and saying it cannot be corroborated again",
              "cannot be corroborated" in f["detail"].lower(), f["detail"][:160])
        check("with a way out that needs a person", "review.py" in f["action"], f["action"])


def test_the_report_is_written_where_findings_looks() -> None:
    src = (ROOT / "tools/corroborate.py").read_text(encoding="utf-8")
    check("the corroborator writes its refusals as a fact",
          "corroboration.json" in src and "atomic.write_json" in src,
          "stdout is swallowed by the tick's log")
    check("and only on a real run, not a dry one", "if not dry:" in src,
          "a dry run must not overwrite the record of the last real one")
    fin = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    check("and the findings builder reads it", "corroboration.json" in fin)
    # By INVOCATION, not by offset. `tick.index(...)` found the mention of
    # `build_findings.py` in the `step` helper's header comment — which sits
    # above everything — and failed an ordering assertion that nothing had
    # broken. The fourth assertion in one sitting to break on that shape, which
    # is why `tests/tick_reader.py` exists instead of a fourth hand-fix.
    sys.path.insert(0, str(ROOT / "tests"))
    import tick_reader
    ok, why = tick_reader.runs_before(tick_reader.tick(ROOT), "corroborate.py",
                                      "build_findings.py")
    check("with corroboration running before findings in the tick", ok, why)


# ─────────────────────── lessons: a second project ──────────────────────

def _lesson_store(repos: dict[str, pathlib.Path | None]):
    """A registry naming these clones and a fresh store, with the modules reloaded on it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-corr-lesson-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "registry/repositories.json").write_text(json.dumps({
        "schema_version": 2,
        "repositories": [{"id": f"repository:fixture/{n}", "name_with_owner": f"fixture/{n}",
                          "host": "github", "local": {"path": str(p)} if p else None}
                         for n, p in repos.items()]}), encoding="utf-8")
    os.environ["OBSERVATORY_REGISTRY"] = str(d / "registry")
    os.environ["OBSERVATORY_DB"] = str(d / "observatory.db")
    os.environ["OBSERVATORY_SCRATCH"] = str(d / "scratch")
    import paths
    importlib.reload(paths)
    from store import db as sdb
    importlib.reload(sdb)
    from store import ledger as L
    importlib.reload(L)
    from store import lessons as LS
    importlib.reload(LS)
    return d, sdb, L, LS


def _other_repo() -> pathlib.Path:
    """A second repository with a root of its own (two `repo_with_history` made in one
    second share every byte of their root commit, and so their project key)."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-corr-other-"))
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "fixture@example.invalid")
    git(d, "config", "user.name", "Fixture")
    (d / "b.txt").write_text("another project\n", encoding="utf-8")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "another root")
    return d


def _key(repo: pathlib.Path) -> str:
    return "p-" + sorted(git(repo, "rev-list", "--max-parents=0", "HEAD").split())[0][:12]


def _write_lesson(conn, L, LS, project: str, commit: str, class_key: str = "absence-needs-its-own-check"):
    body = LS.body_of({"classKey": class_key, "title": "An absent input must fail its own check",
                       "trigger": "a gate reads an optional file",
                       "check": "docs gate rule absent-file",
                       "nouns": ["gate", "absence", "file"],
                       "origin": {"projectId": project, "commit": commit}})
    out = LS.write(conn, L, owner="agent:task-pipeline", body=body,
                   provenance=[{"source": "test"}])
    conn.commit()
    return out


def _state(conn, mid: str) -> str:
    return conn.execute("SELECT state FROM ledger WHERE memory_id = ? ORDER BY revision DESC"
                        " LIMIT 1", (mid,)).fetchone()[0]


def test_two_projects_with_one_class_promote_each_other() -> None:
    a, a1, _ = repo_with_history()
    b = _other_repo()
    d, sdb, L, LS = _lesson_store({"alpha": a, "beta": b})
    conn = sdb.connect()
    la = _write_lesson(conn, L, LS, _key(a), a1)
    lb = _write_lesson(conn, L, LS, _key(b), git(b, "rev-parse", "HEAD"))
    conn.close()
    check("two clones carry two distinct project keys", _key(a) != _key(b), _key(a))
    p = subprocess.run([PY, "tools/corroborate.py"], cwd=ROOT, env=dict(os.environ),
                       capture_output=True, text=True, timeout=300)
    check("the corroborator ran", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    conn = sdb.connect()
    check("the first project's lesson is observed", _state(conn, la["memoryId"]) == "observed",
          _state(conn, la["memoryId"]))
    check("and the second's", _state(conn, lb["memoryId"]) == "observed",
          _state(conn, lb["memoryId"]))
    row = conn.execute("SELECT owner, provenance_json, evidence_json FROM ledger WHERE"
                       " memory_id = ? ORDER BY revision DESC LIMIT 1",
                       (la["memoryId"],)).fetchone()
    check("its author still owns it", row[0] == "agent:task-pipeline", row[0])
    check("the lesson corroborator is named in provenance",
          "agent:lesson-corroborator" in row[1], row[1][:200])
    check("and the check names the witness and both resolutions",
          lb["memoryId"] in row[2] and "resolves" in row[2], row[2][:300])
    conn.close()
    receipt = json.loads((d / "scratch/corroboration.json").read_text(encoding="utf-8"))
    check("the receipt counts lessons apart from refusals",
          receipt["counts"]["lessons_promoted"] == 2 and receipt["refused"] == [],
          json.dumps(receipt["counts"]))


def test_one_project_twice_is_one_witness() -> None:
    a, a1, a2 = repo_with_history()
    d, sdb, L, LS = _lesson_store({"alpha": a})
    conn = sdb.connect()
    first = _write_lesson(conn, L, LS, _key(a), a1)
    again = _write_lesson(conn, L, LS, _key(a), a2)
    check("a repeat from the same project revises, never mints",
          again["memoryId"] == first["memoryId"] and again["outcome"] == "revised", str(again))
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("and is not promoted by itself", _state(conn, first["memoryId"]) == "proposed",
          _state(conn, first["memoryId"]))
    conn.close()
    receipt = json.loads((d / "scratch/corroboration.json").read_text(encoding="utf-8"))
    check("it waits for a second project",
          [w["memory_id"] for w in receipt["lessons"]["waiting"]] == [first["memoryId"]],
          json.dumps(receipt["lessons"]))


def test_a_commit_git_cannot_find_blocks_the_promotion() -> None:
    a, a1, _ = repo_with_history()
    d, sdb, L, LS = _lesson_store({"alpha": a})
    conn = sdb.connect()
    ghost = _write_lesson(conn, L, LS, _key(a), "deadbeefcafe")
    elsewhere = _write_lesson(conn, L, LS, "p-0123456789ab", "abcdef1234567")
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("a lesson whose commit is not in its project's clone stays proposed",
          _state(conn, ghost["memoryId"]) == "proposed", _state(conn, ghost["memoryId"]))
    check("and cannot witness for another project",
          _state(conn, elsewhere["memoryId"]) == "proposed",
          _state(conn, elsewhere["memoryId"]))
    conn.close()
    receipt = json.loads((d / "scratch/corroboration.json").read_text(encoding="utf-8"))
    check("the contradiction is in the receipt, with git's reason",
          [c["memory_id"] for c in receipt["lessons"]["contradicted"]] == [ghost["memoryId"]]
          and "not an object" in receipt["lessons"]["contradicted"][0]["why"],
          json.dumps(receipt["lessons"]))


def test_a_project_with_no_clone_here_is_not_a_witness() -> None:
    """The operator's rule (O2): both commits must RESOLVE. A project with no clone in
    the registry cannot be asked, so its lesson witnesses nothing and stays proposed, and
    the receipt says why."""
    a, a1, _ = repo_with_history()
    d, sdb, L, LS = _lesson_store({"alpha": a})
    conn = sdb.connect()
    here = _write_lesson(conn, L, LS, _key(a), a1)
    away = _write_lesson(conn, L, LS, "p-0123456789ab", "abcdef1234567")
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("a resolvable lesson with only an unknown project beside it stays proposed",
          _state(conn, here["memoryId"]) == "proposed", _state(conn, here["memoryId"]))
    check("and so does the lesson nobody here can ask",
          _state(conn, away["memoryId"]) == "proposed", _state(conn, away["memoryId"]))
    conn.close()
    receipt = json.loads((d / "scratch/corroboration.json").read_text(encoding="utf-8"))
    unres = receipt["lessons"].get("unresolvable", [])
    check("the receipt lists it as not resolvable here, with the reason",
          [u["memory_id"] for u in unres] == [away["memoryId"]]
          and "commit not resolvable here" in unres[0]["why"], json.dumps(receipt["lessons"]))
    check("and its count", receipt["counts"].get("lessons_unresolvable") == 1,
          json.dumps(receipt["counts"]))


def test_two_invented_keys_promote_nothing() -> None:
    """One caller writing two made-up `p-` keys must not buy `observed`."""
    d, sdb, L, LS = _lesson_store({})
    conn = sdb.connect()
    one = _write_lesson(conn, L, LS, "p-0123456789ab", "abcdef1234567")
    two = _write_lesson(conn, L, LS, "p-ba9876543210", "1234567abcdef")
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("neither invented project is promoted",
          {_state(conn, one["memoryId"]), _state(conn, two["memoryId"])} == {"proposed"},
          f"{_state(conn, one['memoryId'])}, {_state(conn, two['memoryId'])}")
    conn.close()


def test_the_project_threshold_is_the_one_constant() -> None:
    """LESSON_PROJECTS is read, not decoration: at three, two projects promote nothing."""
    a, a1, _ = repo_with_history()
    b = _other_repo()
    d, sdb, L, LS = _lesson_store({"alpha": a, "beta": b})
    conn = sdb.connect()
    la = _write_lesson(conn, L, LS, _key(a), a1)
    _write_lesson(conn, L, LS, _key(b), git(b, "rev-parse", "HEAD"))
    conn.close()
    mod = load_corroborator()
    mod.LESSON_PROJECTS = 3
    mod.main(["corroborate.py"])
    conn = sdb.connect()
    check("with the threshold at three, two projects leave it proposed",
          _state(conn, la["memoryId"]) == "proposed", _state(conn, la["memoryId"]))
    conn.close()


def test_the_proposed_count_is_the_rows_the_session_rule_looked_at() -> None:
    a, a1, _ = repo_with_history()
    d, sdb, L, LS = _lesson_store({"alpha": a})
    conn = sdb.connect()
    _write_lesson(conn, L, LS, _key(a), a1)
    conn.close()
    load_corroborator().main(["corroborate.py"])
    receipt = json.loads((d / "scratch/corroboration.json").read_text(encoding="utf-8"))
    check("counts.proposed leaves out the lessons, which have their own counts",
          receipt["counts"]["proposed"] == 0 and receipt["counts"]["lessons_waiting"] == 1,
          json.dumps(receipt["counts"]))


def test_a_promoted_lesson_whose_commit_changes_is_proposed_again() -> None:
    """A revision that cites a different commit is a new claim: the corroboration was of
    the old commit. It drops back to `proposed` and is promoted again only once the new
    commit resolves."""
    a, a1, _ = repo_with_history()
    b = _other_repo()
    d, sdb, L, LS = _lesson_store({"alpha": a, "beta": b})
    conn = sdb.connect()
    _write_lesson(conn, L, LS, _key(a), a1)
    lb = _write_lesson(conn, L, LS, _key(b), git(b, "rev-parse", "HEAD"))
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("both start observed", _state(conn, lb["memoryId"]) == "observed",
          _state(conn, lb["memoryId"]))

    def bs_record():
        row = LS.existing(conn, "absence-needs-its-own-check", _key(b))
        return (row["memory_id"], row["state"]) if row is not None else (None, None)

    moved = _write_lesson(conn, L, LS, _key(b), "deadbeefcafe")
    check("a new commit after promotion is not kept as observed",
          bs_record()[1] == "proposed", f"{moved.get('outcome')}: {bs_record()}")
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("and a commit git cannot find keeps it proposed", bs_record()[1] == "proposed",
          str(bs_record()))
    (b / "c.txt").write_text("more\n", encoding="utf-8")
    git(b, "add", "-A")
    git(b, "commit", "-qm", "the fix")
    _write_lesson(conn, L, LS, _key(b), git(b, "rev-parse", "HEAD"))
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    check("it is promoted again once the new commit resolves", bs_record()[1] == "observed",
          str(bs_record()))
    conn.close()

def test_observation_is_revalidated_when_unchanged_commit_disappears() -> None:
    a, a1, _ = repo_with_history()
    b = _other_repo()
    (b / "extra").write_text("evidence")
    git(b, "add", "extra")
    git(b, "commit", "-qm", "evidence")
    evidence = git(b, "rev-parse", "HEAD")
    d, sdb, L, LS = _lesson_store({"alpha": a, "beta": b})
    conn = sdb.connect()
    _write_lesson(conn, L, LS, _key(a), a1)
    item = _write_lesson(conn, L, LS, _key(b), evidence)
    conn.close()
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    prior = L.current(conn, item["memoryId"])
    body = json.loads(prior["body_json"])
    body["title"] = "A revised description with two resolving projects"
    revised = LS.write(conn, L, owner=prior["owner"], body=body, provenance=[])
    check("valid unchanged evidence keeps a wording revision observed", revised["state"] == "observed")
    conn.close()
    git(b, "reset", "--hard", "HEAD~1")
    git(b, "reflog", "expire", "--expire=now", "--all")
    git(b, "gc", "--prune=now")
    load_corroborator().main(["corroborate.py"])
    conn = sdb.connect()
    try:
        current = LS.existing(conn, "absence-needs-its-own-check", _key(b))
        check("a vanished unchanged commit no longer carries observed promotion",
              current is not None and current["state"] == "proposed", str(dict(current) if current else None))
        check("the old observation remains archived history", _state(conn, item["memoryId"]) == "archived")
    finally:
        conn.close()


if __name__ == "__main__":
    print("the second witness — what it refuses, and who hears about it\n")
    for fn in (test_a_row_with_no_recorded_head_is_refused,
               test_a_missing_clone_is_refused,
               test_a_rewritten_history_is_refused,
               test_a_sha_that_fell_off_its_branch_is_refused,
               test_the_check_verifies_BOTH_existence_and_ancestry,
               test_only_session_rows_are_eligible_and_the_rule_is_singular,
               test_the_ledger_still_refuses_a_self_witness,
               test_a_refusal_becomes_a_finding,
               test_the_report_is_written_where_findings_looks,
               test_two_projects_with_one_class_promote_each_other,
               test_one_project_twice_is_one_witness,
               test_a_commit_git_cannot_find_blocks_the_promotion,
               test_a_project_with_no_clone_here_is_not_a_witness,
               test_two_invented_keys_promote_nothing,
               test_the_project_threshold_is_the_one_constant,
               test_the_proposed_count_is_the_rows_the_session_rule_looked_at,
               test_a_promoted_lesson_whose_commit_changes_is_proposed_again,
               test_observation_is_revalidated_when_unchanged_commit_disappears):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery refusal has now been watched, and each one reaches a person\033[0m")
