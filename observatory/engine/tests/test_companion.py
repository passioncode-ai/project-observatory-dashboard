#!/usr/bin/env python3
"""The one component that runs outside this repository, and it had stopped.

`skill/plugins/observatory-log` installs a Stop hook into every session of every
watched project. It is deliberately silent about anything that is not its
business — no observatory checkout, not a git repository, not in the registry,
nothing changed — because a hook that reports its own confusion teaches the
operator to disable hooks. **That silence hid a total failure for fifteen
hours.**

Measured 2026-09-07, in this repository's own session:

    newest `session` ledger row   2026-09-06T20:12:52Z   (revision 2)
    Stop hooks that ran since     ~72   (from the session transcript)
    rows written                  0

The cause, once the recorder was driven by hand with the real session id:

    IllegalTransition: mem:4f2fb68d9d574530: observed -> proposed is not a
    lifecycle edge; from observed the legal moves are ['archived', 'supported']

`tools/corroborate.py` promotes a `proposed` session row to `observed` once it
can re-check the commit sha — which is exactly its job. The recorder then asked
for `state="proposed"` on the promoted record, the lifecycle refused, the
handler turned it into `recorded: false`, `ask-why.py` said nothing on a false,
and the hook discards stderr. **So once a session was corroborated, the
companion stopped recording it for ever, silently.**

Revising the record in place at its current state would be worse than the bug:
the new statement describes work the corroborator never checked, so keeping
`observed` would claim a corroboration the text never got. A fresh `proposed`
record is what an automated writer may make, and the link travels in
`provenance.continues` — not `supersedes`, which would claim the earlier record
is no longer true when it is still true about earlier work.

**And the fix to the hook could not have taken effect.** A plugin whose
marketplace source is a local `directory` is COPIED into
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` at install time and
the harness runs the copy: the repository's hook was edited at 04:22 and the
copy from 2026-09-03 was still running at 11:30. The copy also FLATTENS the
plugin directory to its root, so the hook's `${CLAUDE_PLUGIN_ROOT}/../../..`
resolved to `~/.claude/plugins` — it found the checkout only because of a
`$HOME/DATA` fallback, on this machine alone.
"""
from __future__ import annotations
import importlib, json, os, pathlib, shutil, sqlite3, subprocess, sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
HOOK = ROOT / "skill/plugins/observatory-log/hooks/record-turn.sh"
ASK = ROOT / "skill/plugins/observatory-log/hooks/ask-why.py"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


#: THE FIXTURE'S OWN CHECKOUT, not the one these tests run inside. The
#: recorder only records a checkout that has uncommitted files or unpushed
#: commits, so assertions aimed at the suite's own tree pass while it is
#: mid-work and go red the moment it is clean and pushed. A fixture that
#: depends on ambient state is not a fixture; `watched_build` below builds a
#: real git repository, a registry that names it, and the change.
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


def workspace() -> tuple[pathlib.Path, dict]:
    d, repo, env = watched_build()
    _CHECKOUT[id(env)] = repo
    return d, env


#: env -> the checkout that env describes, so `record()` keeps its two-argument
#: shape and every existing call site still reads.
_CHECKOUT: dict[int, pathlib.Path] = {}


def record(env: dict, session: str, cwd: str | None = None):
    where = cwd or str(_CHECKOUT.get(id(env), ROOT))
    return subprocess.run(
        [PY, "tools/record_turn.py", "--cwd", where, "--session-id", session],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def rows(d: pathlib.Path, sql: str) -> list:
    f = d / "observatory.db"
    if not f.is_file():
        return []
    conn = sqlite3.connect(f)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ─────────── a promoted record is continued, not downgraded ────────────

def test_a_corroborated_session_keeps_being_recorded() -> None:
    """THE MEASURED DEFECT, driven end to end."""
    d, env = workspace()
    first = record(env, "s-1")
    got = json.loads(first.stdout or "{}")
    check("the first turn records", got.get("recorded") is True, first.stdout[-200:])
    mid = got.get("memoryId")

    # Promote it exactly as `tools/corroborate.py` would.
    conn = sqlite3.connect(d / "observatory.db")
    sys.path.insert(0, str(ROOT))
    import paths
    with conn:
        conn.execute("UPDATE ledger SET state = 'observed' WHERE memory_id = ?", (mid,))
    conn.close()

    second = record(env, "s-1")
    got2 = json.loads(second.stdout or "{}")
    check("the next turn is still recorded", got2.get("recorded") is True,
          second.stdout[-260:])
    check("as a NEW record rather than a downgrade of the promoted one",
          got2.get("memoryId") != mid, f"{mid} vs {got2.get('memoryId')}")
    check("and it says what it continues", got2.get("continues") == mid,
          str(got2.get("continues")))
    kept = rows(d, f"SELECT state FROM ledger WHERE memory_id = '{mid}'")
    check("the corroborated record keeps its state",
          [r["state"] for r in kept] == ["observed"], str([r["state"] for r in kept]))
    fresh = rows(d, "SELECT state, provenance_json FROM ledger"
                    f" WHERE memory_id = '{got2.get('memoryId')}'")
    check("the continuation is `proposed`, which is all an agent may write",
          fresh and fresh[0]["state"] == "proposed",
          str([r["state"] for r in fresh]))
    check("and the link is in provenance, not in supersedes",
          fresh and mid in (fresh[0]["provenance_json"] or ""),
          str(fresh[0]["provenance_json"])[:160] if fresh else "")


def test_a_second_turn_of_a_live_record_still_revises_it() -> None:
    """The fix must not turn every turn into a new record — that is the defect
    the session id exists to prevent, and one long session already holds twenty
    revisions of one memory."""
    d, env = workspace()
    a = json.loads(record(env, "s-2").stdout or "{}")
    # Change the tree's summary without changing the tree: the statement is
    # built from a git diff, so a second call with the same tree is refused as
    # "unchanged since the last turn" — which is itself the right answer.
    b = json.loads(record(env, "s-2").stdout or "{}")
    check("an unchanged tree is not appended again",
          b.get("recorded") is False and "unchanged" in (b.get("reason") or ""),
          str(b))
    check("and it is not reported as a fault", b.get("fault") in (None, False), str(b))
    conn = sqlite3.connect(d / "observatory.db")
    with conn:
        conn.execute("UPDATE ledger SET statement = 'something else'"
                     " WHERE memory_id = ?", (a.get("memoryId"),))
    conn.close()
    c = json.loads(record(env, "s-2").stdout or "{}")
    check("a changed tree revises the SAME record",
          c.get("memoryId") == a.get("memoryId"), f"{a.get('memoryId')} vs {c.get('memoryId')}")
    check("at the next revision", c.get("revision") == 2, str(c.get("revision")))


# ─────────── a fault is not a quiet turn ───────────────────────────────

def test_the_receipt_distinguishes_a_fault_from_an_answer() -> None:
    d, env = workspace()
    record(env, "s-3", cwd="/tmp")
    doc = json.loads((d / "scratch/record-turn.json").read_text(encoding="utf-8"))
    check("a receipt exists on the quiet path", bool(doc), str(doc))
    check("`not a git repository` is not a fault", doc.get("fault") is False, str(doc))
    check("and it is stamped", bool(doc.get("at")), str(doc))

    d2, env2 = workspace()
    # `--cwd` IS THE FIXTURE'S CHECKOUT, not `.`. Driving the recorder at this
    # repository made the planted IllegalTransition unreachable the moment the
    # tree was clean: the run returned "nothing changed" long before it reached
    # the ledger, and the receipt said `fault: false` about a fault that never
    # got the chance to happen (2026-09-09).
    watched = _CHECKOUT[id(env2)]
    code = ("import sys; sys.path.insert(0, '.')\n"
            "from store import ledger as L\n"
            "L.append = lambda *a, **k: (_ for _ in ()).throw("
            "L.IllegalTransition('planted: observed -> proposed'))\n"
            f"sys.argv = ['record_turn.py', '--cwd', {str(watched)!r}, '--session-id', 's']\n"
            "import tools.record_turn as R\n"
            "R.main()\n")
    subprocess.run([PY, "-c", code], cwd=ROOT, env=env2, capture_output=True,
                   text=True, timeout=300)
    doc = json.loads((d2 / "scratch/record-turn.json").read_text(encoding="utf-8"))
    check("an IllegalTransition IS a fault", doc.get("fault") is True, str(doc))
    check("with the reason kept", "IllegalTransition" in (doc.get("reason") or ""),
          str(doc))


def test_ask_why_speaks_on_a_fault_and_stays_quiet_otherwise() -> None:
    def ask(payload: dict) -> str:
        p = subprocess.run([PY, str(ASK)], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60)
        return p.stdout
    check("a quiet turn stays quiet",
          ask({"recorded": False, "reason": "nothing changed", "fault": False}) == "",
          "a hook that speaks about nothing teaches the operator to disable it")
    spoke = ask({"recorded": False, "fault": True,
                 "reason": "IllegalTransition: observed -> proposed"})
    check("a fault is spoken", "could NOT record" in spoke, spoke[:160])
    check("with the reason in it", "IllegalTransition" in spoke, spoke[:160])
    check("and it still does not block",
          '"decision"' not in spoke and '"block"' not in spoke, spoke[:160])
    check("an explained record says nothing",
          ask({"recorded": True, "hasWhy": True, "memoryId": "m", "revision": 1}) == "",
          "asking again for a why that exists is noise")


def test_the_receipt_says_whose_turn_it_describes() -> None:
    """`store/raw/record-turn.json` is written by EVERY session of EVERY watched
    project on the machine — 49 Claude processes were running when that was
    measured — so the last writer wins and a reader must be able to tell whose
    turn the reason describes."""
    d, env = workspace()
    record(env, "session-abc", cwd="/tmp")
    doc = json.loads((d / "scratch/record-turn.json").read_text(encoding="utf-8"))
    check("the session is stamped even on the earliest guard",
          doc.get("session") == "session-abc", str(doc))
    import observatory as obs
    importlib.reload(obs)
    # `FOREIGN_WRITES_IGNORED`, not `IGNORED_WRITES_ALLOWED`. The receipt is not
    # something a gate STEP may write — no step of `check` records a turn — it is
    # written by a process that is not the gate at all, and the two claims were
    # split apart after the single dict made its own "nothing under store/raw"
    # ban a lie about its first entry.
    check("and the gate does not treat a concurrently-written file as impurity",
          "store/raw/record-turn.json" in obs.FOREIGN_WRITES_IGNORED,
          str(sorted(obs.FOREIGN_WRITES_IGNORED)))
    check("as a foreign write rather than a step's own output",
          "store/raw/record-turn.json" not in obs.IGNORED_WRITES_ALLOWED,
          "no step of `check` records a turn")
    why = obs.FOREIGN_WRITES_IGNORED.get("store/raw/record-turn.json", "")
    check("with the reason stated where the exemption is",
          "every session" in why.lower(), why[:120])


def test_a_fault_becomes_a_finding() -> None:
    """END TO END through `collect()`, and the finding now depends on WHERE the
    fault is.

    The receipt is a slot: one document, overwritten by the next turn of any
    session on the machine. Since faults are journalled, the durable record is
    `companion-faults.jsonl`, so a fault that exists ONLY in the receipt means
    the append-only log could not keep it — a different fact with a different
    remedy, and reported as one. Both branches are driven here; the rule's own
    arithmetic is driven in `tests/test_companion_faults.py`.
    """
    d, env = workspace()
    (d / "registry").mkdir(exist_ok=True)
    (d / "registry/projects.json").write_text('{"projects": []}')
    # STAMPED FROM NOW. The rule compares the fault's age against a window, so a
    # literal date would make this pass today and fail next week.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / "scratch/record-turn.json").write_text(json.dumps({
        "recorded": False, "reason": "IllegalTransition: observed -> proposed",
        "at": now, "session": "session-xyz", "fault": True}))
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"))
    import paths
    importlib.reload(paths)
    import companion_faults
    importlib.reload(companion_faults)
    import build_findings as B
    importlib.reload(B)
    got = [f for f in B.collect() if f["type"] == "companion.faults_unlogged"]
    check("a fault only the receipt holds is reported as one the log lost",
          len(got) == 1, str(got)[:200])
    if got:
        check("and it says the receipt is now the only copy",
              "only copy" in got[0]["action"], got[0]["action"][:160])

    companion_faults.append({"at": now, "session": "session-xyz",
                             "reason": "IllegalTransition: observed -> proposed",
                             "cwd": str(d)})
    importlib.reload(B)
    found = B.collect()
    got = [f for f in found if f["type"] == "companion.not_recording"]
    check("the same fault in the durable log raises the counted finding",
          len(got) == 1, str([f["type"] for f in found])[:200])
    if got:
        check("it says the session's later work is absent",
              "absent from the ledger" in got[0]["detail"], got[0]["detail"][-120:])
    check("and it is not reported twice",
          not [f for f in found if f["type"] == "companion.faults_unlogged"],
          "one fault, one row")
    for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB"):
        os.environ.pop(k, None)
    importlib.reload(paths)
    importlib.reload(companion_faults)


# ─────────── the installed copy is what runs ───────────────────────────

def test_a_diverged_install_is_a_finding() -> None:
    """The defect that made a morning's fix inert. The install path is a module
    constant precisely so this can be driven."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-install-"))
    (d / "registry").mkdir(exist_ok=True)
    (d / "scratch").mkdir()
    (d / "registry/projects.json").write_text('{"projects": []}')
    fake = d / "cache/observatory-log/0.1.0/hooks"
    fake.mkdir(parents=True)
    os.environ.update(OBSERVATORY_REGISTRY=str(d / "registry"),
                      OBSERVATORY_SCRATCH=str(d / "scratch"),
                      OBSERVATORY_DB=str(d / "absent.db"),
                      OBSERVATORY_COMPANION_INSTALL=str(d / "cache/observatory-log"))
    import paths
    importlib.reload(paths)
    import build_findings as B
    importlib.reload(B)

    shutil.copy(HOOK, fake / "record-turn.sh")
    check("a matching install raises nothing",
          [f for f in B.collect() if f["type"] == "companion.stale_install"] == [], "")
    (fake / "record-turn.sh").write_text("#!/usr/bin/env bash\n# an older copy\n")
    got = [f for f in B.collect() if f["type"] == "companion.stale_install"]
    check("a diverged install IS a finding", len(got) == 1, str(got)[:200])
    if got:
        check("it says the harness runs the copy", "runs the COPY" in got[0]["detail"],
              got[0]["detail"][:120])
        check("and that a restart is part of the remedy",
              "restart" in got[0]["action"], got[0]["action"])
    for k in ("OBSERVATORY_REGISTRY", "OBSERVATORY_SCRATCH", "OBSERVATORY_DB",
              "OBSERVATORY_COMPANION_INSTALL"):
        os.environ.pop(k, None)
    importlib.reload(paths)


def test_the_hook_finds_the_checkout_from_a_flattened_install() -> None:
    """`${CLAUDE_PLUGIN_ROOT}/../../..` is the checkout only when the plugin runs
    from the repository. The install flattens the plugin to the cache root, so
    the fixed depth landed on `~/.claude/plugins` and only a `$HOME/DATA`
    fallback saved it — on this machine alone."""
    # NO FALLBACK. The first version wrote
    # `tick_reader.code_only(...) if hasattr(...) else raw_text`, and the
    # `hasattr` was false — so the assertion read the RAW file, matched the
    # comment explaining the fix, and reported the fix as missing. A guard that
    # degrades to the wrong reading is worse than an ImportError, which at least
    # names itself.
    import tick_reader
    src = tick_reader.code_only(HOOK.read_text(encoding="utf-8"))
    check("the fixed three-level guess is gone",
          "/../../.." not in src, "a depth is a fact about one layout")
    check("a walk upward replaces it", "for _ in 1 2 3 4 5 6" in src, "")

    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-flat-"))
    (d / "scratch").mkdir()
    flat = d / "cache/mkt/plugin/0.1.0/hooks"
    flat.mkdir(parents=True)
    shutil.copy(HOOK, flat / "record-turn.sh")
    (flat / "record-turn.sh").chmod(0o755)
    shutil.copy(ASK, flat / "ask-why.py")
    # THE SUBJECT IS A REPOSITORY THE FIXTURE OWNS, not the one this runs inside.
    # `cwd = ROOT` made the assertion depend on the OPERATOR'S working tree
    # being dirty: the hook declines with `nothing changed` when there is
    # nothing to record — its own description says so — so the check passed
    # only while somebody had uncommitted work, and went red the moment a
    # session committed (two gate runs apart with nothing between them but a
    # commit). `watched_build` builds a real git repository with a real change
    # plus a registry that names it, which is the same remedy other suites took.
    _work, checkout, wenv = watched_build()
    env = dict(wenv, CLAUDE_PLUGIN_ROOT=str(flat.parent), OBSERVATORY_ROOT=str(ROOT))
    d = pathlib.Path(str(_work))
    p = subprocess.run(["bash", str(flat / "record-turn.sh")],
                       input=json.dumps({"session_id": "flat-probe",
                                         "cwd": str(checkout),
                                         "stop_hook_active": False}),
                       env=env, capture_output=True, text=True, timeout=300)
    check("the hook records from a flattened install", p.returncode == 0,
          (p.stdout + p.stderr)[-200:])
    check("and a row landed", bool(rows(d, "SELECT 1 FROM ledger")),
          (p.stdout or "")[:160])


def test_the_hook_is_silent_about_what_is_not_its_business() -> None:
    """The property the silence exists for, still true — a hook installed
    globally must change nothing in a project the observatory does not watch."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-quiet-"))
    (d / "scratch").mkdir()
    (d / "elsewhere").mkdir()
    env = dict(os.environ, OBSERVATORY_ROOT=str(ROOT),
               OBSERVATORY_DB=str(d / "observatory.db"),
               OBSERVATORY_SCRATCH=str(d / "scratch"))
    p = subprocess.run(["bash", str(HOOK)],
                       input=json.dumps({"session_id": "quiet", "cwd": str(d / "elsewhere"),
                                         "stop_hook_active": False}),
                       env=env, capture_output=True, text=True, timeout=300)
    check("nothing is said about an unwatched directory", p.stdout.strip() == "",
          p.stdout[:200])
    check("and it exits clean", p.returncode == 0, str(p.returncode))
    p2 = subprocess.run(["bash", str(HOOK)],
                        input=json.dumps({"session_id": "quiet", "cwd": str(ROOT),
                                          "stop_hook_active": True}),
                        env=env, capture_output=True, text=True, timeout=300)
    check("a re-entrant Stop hook does nothing", p2.stdout.strip() == "", p2.stdout[:160])


def test_the_ask_nudges_when_the_projects_knowledge_is_stale() -> None:
    """The graph and the wiki age travel with the record, and the hook speaks.

    Both halves: a fixture with an OLD graph and OLD notes produces the nudge;
    the same fixture without them produces none — a project that never adopted
    graphify is making a choice, not rotting.
    """
    import os as _os
    import subprocess as _sp
    d, env = workspace()
    repo = _CHECKOUT[id(env)]
    quiet = record(env, "s-fresh")
    got = json.loads(quiet.stdout or "{}")
    check("with no graph and no vault receipt both ages are None",
          got.get("graphAgeDays") is None and got.get("wikiAgeDays") is None,
          quiet.stdout[-200:])

    (repo / "graphify-out").mkdir()
    gj = repo / "graphify-out/graph.json"
    gj.write_text("{}", encoding="utf-8")
    _os.utime(gj, (1, 1))                              # 1970: as old as it gets
    scratch = pathlib.Path(env["OBSERVATORY_SCRATCH"])
    (scratch / "vault.json").write_text(json.dumps([
        {"folder": repo.name, "notes_updated_on": "2020-01-01"}]), encoding="utf-8")
    (repo / "more.txt").write_text("another change\n", encoding="utf-8")
    second = record(env, "s-stale")
    got = json.loads(second.stdout or "{}")
    check("an old graph is measured in days", (got.get("graphAgeDays") or 0) > 365,
          second.stdout[-200:])
    check("and so are the old notes", (got.get("wikiAgeDays") or 0) > 365,
          second.stdout[-200:])
    hook = ROOT / "skill/plugins/observatory-log/hooks/ask-why.py"
    p2 = _sp.run([PY, str(hook)], input=second.stdout, capture_output=True,
                 text=True, timeout=60)
    msg = (json.loads(p2.stdout or "{}").get("systemMessage") or "")
    check("the ask carries the graph nudge", "/graphify . --update" in msg, msg[-300:])
    check("and the wiki nudge", "wiki notes" in msg, msg[-300:])
    p3 = _sp.run([PY, str(hook)], input=quiet.stdout, capture_output=True,
                 text=True, timeout=60)
    msg3 = (json.loads(p3.stdout or "{}").get("systemMessage") or "")
    check("and a fresh project gets no nudge", "graphify" not in msg3,
          msg3[-200:])


if __name__ == "__main__":
    print("the companion — the recorder that had stopped, and the copy that runs\n")
    for fn in (test_a_corroborated_session_keeps_being_recorded,
               test_a_second_turn_of_a_live_record_still_revises_it,
               test_the_receipt_distinguishes_a_fault_from_an_answer,
               test_ask_why_speaks_on_a_fault_and_stays_quiet_otherwise,
               test_the_receipt_says_whose_turn_it_describes,
               test_a_fault_becomes_a_finding,
               test_a_diverged_install_is_a_finding,
               test_the_hook_finds_the_checkout_from_a_flattened_install,
               test_the_hook_is_silent_about_what_is_not_its_business,
               test_the_ask_nudges_when_the_projects_knowledge_is_stale):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma corroborated session keeps being recorded, and a failure to "
          "record says so\033[0m")
