#!/usr/bin/env python3
"""The companion plugin's Stop hook — the only writer whose rows a machine can re-check.

`tools/corroborate.py` promotes exactly one kind of row without a person:
`session`, written by this hook, because it records a repository, a branch and a
HEAD sha that git can be asked about later. Everything the second witness
believes rests on that row being written honestly and exactly once per session.

**The hook checked one interpreter and used another.** `read_field` shelled out
to `python3` from PATH to parse the payload, while only the RECORDING
interpreter below it was ever tested for existence. With no `python3` on the
hook's PATH every field came back empty, silently, and the script carried on —
and an empty `session_id` is not a harmless omission: `tools/record_turn.py`
keys its prior-row lookup on the session, so it would mint a NEW `proposed`
memory on every turn instead of revising one, and the "unchanged since the last
turn" guard could never fire.

The scale is measurable: one session in this store holds **twenty revisions of
one memory**. Without an id that is twenty memories in the review queue, each
needing a human decision — and this repository has repeatedly found silent
multiplication worse than silence.

Measured before changing anything: seven session memories, every one with a
session id, zero rows without. The path is reachable and had not fired here.
"""
from __future__ import annotations
import json, os, pathlib, shutil, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import live_estate                                                  # noqa: E402
import tmp as tmpdir  # noqa: E402
# A synthetic workspace, whoever starts the suite: the last case writes to the
# workspace's own store, and that must never be a real one.
from test_portable_mcp import setup as portable_setup              # noqa: E402
portable_setup()

HOOK = ROOT / "skill/plugins/observatory-log/hooks/record-turn.sh"
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def _watched_git(cwd: pathlib.Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    return p.stdout


def watched_build(dirty: bool = True) -> tuple[pathlib.Path, pathlib.Path, dict]:
    """(workspace, checkout, env) — a git repository the registry knows, with a change.

    The fixture builds its OWN subject rather than pointing the recorder at the
    checkout the suite runs inside: the recorder only records a checkout with
    uncommitted files or unpushed commits, so a suite aimed at its own tree
    passes while the tree is mid-work and fails the moment it is clean. The
    hook refuses anything the registry does not know, so three facts are
    written: a repository matched by `local.path`, an `implemented_by`
    relation to a project, and an ownership the estate records.
    """
    import estate
    ownership = sorted(estate.RECORDED_OWNERSHIP)[0]
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-watched-"))
    repo = work / "checkout"
    repo.mkdir()
    (work / "scratch").mkdir()
    reg = work / "registry"
    reg.mkdir(exist_ok=True)
    _watched_git(repo, "init", "-q", "-b", "main")
    _watched_git(repo, "config", "user.email", "fixture@example.invalid")
    _watched_git(repo, "config", "user.name", "Fixture")
    (repo / "README.md").write_text("a watched project\n", encoding="utf-8")
    _watched_git(repo, "add", "-A")
    _watched_git(repo, "commit", "-qm", "first")
    if dirty:
        # An uncommitted file is what the hook calls a change; a commit would
        # need an upstream to be "unpushed" against.
        (repo / "worked-on.txt").write_text("the turn's work\n", encoding="utf-8")
    top = _watched_git(repo, "rev-parse", "--show-toplevel").strip()
    repo_id, project_id = "repository:fixture/alpha-web", "project:alpha-web"
    (reg / "repositories.json").write_text(json.dumps({
        "schema_version": 1,
        "repositories": [{"id": repo_id, "name_with_owner": "fixture/alpha-web",
                          "host": "github", "local": {"path": top}}]}), encoding="utf-8")
    (reg / "projects.json").write_text(json.dumps({
        "schema_version": 1,
        "projects": [{"id": project_id, "name": "alpha-web", "ownership": ownership,
                      "anchor": "local-folder", "membership_rules": []}]}), encoding="utf-8")
    (reg / "relations.json").write_text(json.dumps({
        "schema_version": 2,
        "relations": [{"id": "relation:alpha-web:implemented-by:fixture-alpha-web",
                       "type": "implemented_by", "from": project_id, "to": repo_id,
                       "source_refs": []}]}), encoding="utf-8")
    env = dict(os.environ, OBSERVATORY_DB=str(work / "observatory.db"),
               OBSERVATORY_SCRATCH=str(work / "scratch"), OBSERVATORY_REGISTRY=str(reg))
    return work, repo, env


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def isolated_store() -> pathlib.Path:
    """Every write in this file goes here. Driving `record_turn.py` against the
    real store put a fixture row in the operator's ledger once already, and it
    had to be removed by hand — the second such slip in one sitting."""
    # AND ITS OWN CHECKOUT. `--cwd ROOT` made every case here depend on this
    # repository being mid-work: the recorder records a checkout with
    # uncommitted files or unpushed commits, so a clean, pushed tree returned
    # "nothing changed" and three suites went red with no code changed
    # A fixture that depends on ambient state is not a fixture, and
    # `watched_build` is the remedy.
    work, repo, env = watched_build()
    _WATCHED[str(work / "observatory.db")] = (repo, env)
    return work / "observatory.db"


#: db path -> (the checkout it describes, its environment). Keeps `record(db,
#: session)` two arguments wide, so no call site below has to change.
_WATCHED: dict[str, tuple] = {}


def record(db: pathlib.Path, session: str) -> dict:
    repo, built = _WATCHED.get(str(db), (ROOT, {}))
    p = subprocess.run([PY, "tools/record_turn.py", "--cwd", str(repo),
                        "--session-id", session], cwd=ROOT,
                       # AND THE SCRATCH. This redirected the store alone, and
                       # was pure by luck: `tools/record_turn.py` wrote nothing
                       # outside it. On 2026-09-07 it gained a receipt — the
                       # thing that makes a silent failure visible — and this
                       # suite immediately began writing a fixture's outcome
                       # into `store/raw/record-turn.json`, which
                       # `tools/build_findings.py` reads. The gate's purity
                       # contract caught it in the same run.
                       # The helper's environment first, then this suite's own
                       # store and scratch on top — `built` already carries both
                       # keys, and passing them twice is a TypeError rather than
                       # an override.
                       env={**os.environ, **built, "OBSERVATORY_DB": str(db),
                            "OBSERVATORY_SCRATCH": str(db.parent / "scratch")},
                       capture_output=True, text=True, timeout=300)
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"_unparseable": (p.stdout + p.stderr)[-200:]}


# ───────────────── the interpreter, resolved once and first ─────────────

def test_the_payload_parser_uses_the_interpreter_the_script_checked() -> None:
    src = HOOK.read_text(encoding="utf-8")
    import source_reader  # noqa: F401  (shell, so read the text — but skip comments)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    check("read_field no longer calls a bare `python3`",
          "| python3 -c" not in body, "an unchecked interpreter parsed the payload")
    check("it uses the resolved interpreter", '| "$py" -c' in body)
    # The engine resolves the interpreter from `OBSERVATORY_PYTHON` (written
    # by `full agent install`) before falling back to the checkout's venv, so
    # the assignment is located by its variable, not by one literal default.
    py_at = body.index('py="${OBSERVATORY_PYTHON:-$root/.venv/bin/python}"')
    read_at = body.index("read_field() {")
    check("and the interpreter is resolved BEFORE the parser is defined",
          py_at < read_at, f"py at {py_at}, read_field at {read_at}")
    check("with a silent exit when there is none",
          '[ -n "$py" ] || exit 0' in body,
          "every other 'not my business' path here is silent; this must be too")


def test_the_reentry_guard_still_comes_before_any_work() -> None:
    """Moving the interpreter resolution up must not move the loop guard down
    past the recording."""
    # By INVOCATION. `index("tools/record_turn.py")` found the EXISTENCE CHECK
    # `[ -f "$root/tools/record_turn.py" ]`, which moved above the guard with
    # the interpreter resolution — the sixth assertion in one sitting to break
    # on "the string is in the file", written minutes after
    # `tests/tick_reader.py` was built for exactly this.
    import tick_reader
    src = HOOK.read_text(encoding="utf-8")
    # `read_field` is an invocation too — the guard calls it, and it is that
    # function which reaches the interpreter. Requiring `"$py"` on the line
    # itself found nothing, because the guard delegates.
    markers = ('"$py"', "read_field")
    guard = tick_reader.first_invocation(src, "stop_hook_active", markers)
    record_at = tick_reader.first_invocation(src, "tools/record_turn.py", markers)
    check("the re-entry guard precedes the recording",
          guard != -1 and record_at != -1 and guard < record_at,
          f"guard at line {guard + 1}, record at line {record_at + 1}")


def test_the_hook_is_silent_about_what_is_not_its_business() -> None:
    """A Stop hook installed globally must change nothing where it does not watch."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-nothook-"))
    p = subprocess.run(["bash", str(HOOK)], cwd=d,
                       env={**os.environ, "OBSERVATORY_ROOT": str(d), "HOME": str(d)},
                       input="", capture_output=True, text=True, timeout=120)
    check("no observatory checkout: exits 0", p.returncode == 0, str(p.returncode))
    check("and says nothing", not p.stdout.strip(), p.stdout[:160])


# ───────────────── one memory per session, not one per turn ─────────────

def test_a_row_with_no_session_is_refused() -> None:
    db = isolated_store()
    out = record(db, "")
    check("the writer refuses", out.get("recorded") is False, str(out)[:200])
    check("and says why in terms of what it would cost",
          "review queue" in out.get("reason", ""), out.get("reason", "")[:120])
    check("nothing was written", not db.is_file() or not sqlite3.connect(
        f"file:{db}?mode=ro", uri=True).execute(
        "SELECT count(*) FROM ledger").fetchone()[0], "a refused write must leave no row")


def test_one_session_revises_one_memory() -> None:
    db = isolated_store()
    first = record(db, "iso-session-1")
    check("the first turn records", first.get("recorded") is True, str(first)[:200])
    again = record(db, "iso-session-1")
    check("an unchanged second turn adds no revision",
          again.get("recorded") is False and "unchanged" in again.get("reason", ""),
          str(again)[:200])
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    mems = con.execute("SELECT count(DISTINCT memory_id) FROM ledger").fetchone()[0]
    check("so the store holds ONE memory, not one per turn", mems == 1, str(mems))
    con.close()


def test_two_sessions_are_two_memories() -> None:
    """The counterpart: the key really is the session, not the project."""
    db = isolated_store()
    record(db, "iso-a")
    record(db, "iso-b")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    mems = con.execute("SELECT count(DISTINCT memory_id) FROM ledger").fetchone()[0]
    check("two sessions, two memories", mems == 2, str(mems))
    con.close()


# ───────────────── the row the second witness will check ────────────────

def test_the_row_carries_what_corroboration_needs() -> None:
    db = isolated_store()
    out = record(db, "iso-evidence")
    if out.get("recorded") is not True:
        check("a row was written to inspect", False, str(out)[:200])
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM ledger WHERE kind='session' LIMIT 1").fetchone()
    ev = json.loads(row["evidence_json"])
    repo = next((e for e in ev if str(e.get("uri", "")).startswith("repo:repository:")), None)
    check("the evidence names a repository", repo is not None, str(ev)[:160])
    if repo:
        check("with the HEAD sha git will be asked about", bool(repo.get("head")), str(repo))
        check("and the branch it must still be an ancestor of",
              bool(repo.get("branch")), str(repo))
    check("the row is `proposed`, never higher", row["state"] == "proposed", row["state"])
    check("owned by the agent that measured it, not the operator",
          row["owner"].startswith("agent:"), row["owner"])
    check("and its provenance says the fact was MEASURED",
          '"measured": true' in row["provenance_json"].lower(), row["provenance_json"][:120])
    con.close()


def test_the_live_store_matches_the_design() -> None:
    """The measurement that showed the defect was latent, kept as a check."""
    import paths
    if not paths.DB.is_file():
        print("  SKIP  no store on this machine")
        return
    con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    idless = con.execute(
        "SELECT count(*) FROM ledger WHERE kind='session'"
        " AND (session_id IS NULL OR session_id = '')").fetchone()[0]
    check("no session row in the live store lacks its session id", idless == 0, str(idless))
    revs = con.execute(
        "SELECT max(c) FROM (SELECT count(*) c FROM ledger WHERE kind='session'"
        " GROUP BY memory_id)").fetchone()[0] or 0
    con.close()
    if not revs:
        # The portable workspace starts with an empty ledger. Give it the
        # history a real one accumulates — one session, two turns with work in
        # between — through the recorder itself, so what is measured below is
        # the recorder's output and not a planted row.
        _work, checkout, env = watched_build()
        env = dict(env, OBSERVATORY_DB=str(paths.DB))
        for turn in ("first", "second"):
            # A new file each turn: the statement summarises the tree, so the
            # second turn must change what the summary says.
            (checkout / f"{turn}-turn.txt").write_text(f"the {turn} turn's work\n",
                                                      encoding="utf-8")
            subprocess.run([PY, "tools/record_turn.py", "--cwd", str(checkout),
                            "--session-id", "workspace-session"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
    con = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    idless = con.execute(
        "SELECT count(*) FROM ledger WHERE kind='session'"
        " AND (session_id IS NULL OR session_id = '')").fetchone()[0]
    check("no session row in the workspace store lacks its session id", idless == 0,
          str(idless))
    revs = con.execute(
        "SELECT max(c) FROM (SELECT count(*) c FROM ledger WHERE kind='session'"
        " GROUP BY memory_id)").fetchone()[0] or 0
    if not live_estate.needs("a ledger with session rows", revs > 0,
                             "the revise-rather-than-append path is driven "
                             "end to end in tests/test_companion.py"):
        return
    check("and a long session is one memory with many revisions", revs > 1,
          f"the deepest holds {revs} revision(s) — one per turn would be one memory each")
    con.close()


if __name__ == "__main__":
    print("the companion hook — one memory per session, or none at all\n")
    for fn in (test_the_payload_parser_uses_the_interpreter_the_script_checked,
               test_the_reentry_guard_still_comes_before_any_work,
               test_the_hook_is_silent_about_what_is_not_its_business,
               test_a_row_with_no_session_is_refused,
               test_one_session_revises_one_memory,
               test_two_sessions_are_two_memories,
               test_the_row_carries_what_corroboration_needs,
               test_the_live_store_matches_the_design):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe hook writes one memory per session, and nothing when it cannot\033[0m")
