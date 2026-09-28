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
               test_the_report_is_written_where_findings_looks):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mevery refusal has now been watched, and each one reaches a person\033[0m")
