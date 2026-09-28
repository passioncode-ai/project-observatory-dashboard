#!/usr/bin/env python3
"""The gate's own promise, enforced against the half git cannot see.

`NON_MUTATING` says a group that answers *"is this tree good?"* must not change
it, and `tree_state()` enforced that with `git status` plus `git diff HEAD`. But
the scratch directory and the store are **gitignored** (and, in a workspace,
outside the tree altogether), so a gate writing them stayed green — and it did.

**What that cost.** The scratch `agent.json` once carried a `halted_by` naming a
mode-644 key file inside a TEST's temp directory that no longer existed.
`tools/build_findings.py` reads that file, so `interpretation.halted` told the
operator the interpretation layer was stopped by a permissions problem in a
fixture — while the real cause was a spend ceiling on the shared key. The
finding named the wrong cause and offered the wrong remedy.

Several suites wrote there, each having redirected some of what a run touches
and not the rest (the store but not the scratch dir), and one ran an ERASURE —
`retention.py apply`, which also VACUUMs the file — against the operator's
store on every gate run, with no redirect at all. No row was lost, which is why
nobody noticed: the harm was potential and the churn invisible.

**And the purity verdict was unreachable whenever anything failed.** It sat after
the step loop, which returned on the first non-zero step — so with one standing
red step the guarantee had not been evaluated at all. The verdict now runs on
every path.

**The database is watched by ROW COUNT, not by bytes**, and that is deliberate:
WAL checkpointing, page reuse, `secure_delete` and a migration applied by any
read-write connect all rewrite pages without changing a single fact. Hashing the
file would report a mutation on every run and teach the reader to ignore it.

The private original drove a whole nested `./observatory.py check` as its
fixture. In the public profile `check` IS the portable runner, which runs this
suite, so here the gate's own `_run_group` is driven with a planted failing step
instead: the same code path, without recursion.
"""
from __future__ import annotations
import hashlib, json, os, pathlib, re, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup               # noqa: E402
portable_setup()
import paths                                                        # noqa: E402
# `tests/tmp.py`, not bare `tempfile`: it registers the cleanup whose absence
# once left thousands of fixture directories holding tens of gigabytes.
import tmp as tmpdir                                                # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def scratch_hashes() -> dict[str, str]:
    """The scratch directory by content. It lives in the selected workspace,
    outside the program tree, so paths are named relative to it."""
    return {str(p.relative_to(paths.SCRATCH)): hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            for p in sorted(paths.SCRATCH.rglob("*")) if p.is_file()}


# ─────────── the check exists and is wired ─────────────────────────────

def test_the_gate_watches_the_ignored_half() -> None:
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    check("there is a snapshot of the ignored artefacts",
          "def ignored_state()" in src)
    check("taken before a NON_MUTATING group runs",
          "before_ignored = ignored_state() if name in NON_MUTATING else None" in src)
    check("and compared after", "files_after, rows_after = ignored_state()" in src)
    check("the database is watched by ROW COUNT, not by bytes",
          "WATCHED_TABLES" in src and "hashlib.sha256(p.read_bytes())" in src,
          "hashing the file would flag WAL churn on every run")
    check("the page is the declared exception, with its reason",
          "docs/projects-dashboard.html" in src and "IGNORED_WRITES_ALLOWED" in src)
    check("and nothing under store/raw is allowed",
          not re.search(r'IGNORED_WRITES_ALLOWED = \{[^}]*store/raw', src, re.S),
          "no step of `check` produces a collector report")
    # A SECOND CONTAINER, because it makes a different claim: not "a step may
    # write this" but "a process that is not the gate writes this". Held apart
    # so the ban above stays true — for one iteration the recorder's receipt
    # lived in `IGNORED_WRITES_ALLOWED` and made that ban a lie about its own
    # first entry, which is how a blanket exemption gets bought with a true
    # observation.
    check("a foreign writer has its own container",
          "FOREIGN_WRITES_IGNORED" in src
          and re.search(r'FOREIGN_WRITES_IGNORED = \{[^}]*store/raw/record-turn\.json',
                        src, re.S) is not None,
          "the companion's receipt is written by another session, not by a step")
    check("and the verdict excludes both",
          "k not in IGNORED_WRITES_ALLOWED" in src
          and "k not in FOREIGN_WRITES_IGNORED" in src)


def test_the_verdict_runs_even_when_a_step_fails() -> None:
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    check("a failing step is recorded rather than returned",
          "failed_at, failed_code = step, code" in src and
          "            break" in src,
          "returning there made the purity check unreachable")
    check("and the group still exits non-zero",
          "return failed_code" in src)
    check("saying the checks ran anyway",
          "purity checks above ran anyway" in src,
          "a reader must know the verdict was evaluated, not skipped")
    # DRIVEN through the gate's own `_run_group`, named `check` so the
    # NON_MUTATING verdict applies, with a planted step that passes and one
    # that fails. Attributable, as a run holding the lease would be.
    steps = {"_planted_pass": [PY, "-c", "print('  PASS  a planted assertion')"],
             "_planted_fail": [PY, "-c", "raise SystemExit(3)"]}
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('obs', %r)\n"
        "obs = importlib.util.module_from_spec(spec); spec.loader.exec_module(obs)\n"
        "obs.STEPS = dict(obs.STEPS); obs.STEPS.update(%r)\n"
        "sys.exit(obs._run_group('check', ['_planted_pass', '_planted_fail'], None,"
        " attributable=True))\n" % (str(ROOT / "observatory.py"), steps))
    p = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True,
                       timeout=600)
    check("the gate reports a failing step", "FAILED at _planted_fail" in p.stderr,
          p.stderr[-200:])
    check("and exits with that step's code", p.returncode == 3, str(p.returncode))
    check("and still reaches its own purity verdict",
          "purity checks above ran anyway" in p.stderr, p.stderr[-300:])
    check("with no ignored artefact written", "wrote:" not in p.stderr,
          [l for l in p.stderr.splitlines() if "wrote:" in l])
    check("and no row count moved", "  rows:  " not in p.stderr,
          [l for l in p.stderr.splitlines() if "rows:" in l])


# ─────────── driven against a planted leak ─────────────────────────────

def test_a_planted_leak_is_caught_and_named() -> None:
    """A check nobody has watched refusing is a green nobody earned — and this
    one refused on its author's own change the first time it ran."""
    probe = paths.SCRATCH / "_purity_probe.json"
    marker = ROOT / "tests" / "_purity_leak.py"
    try:
        marker.write_text(
            "import pathlib, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import paths\n"
            "(paths.SCRATCH / '_purity_probe.json').write_text('{\"leak\": true}')\n"
            "print('  PASS  a suite that writes into the live scratch dir')\n",
            encoding="utf-8")
        # Registered as a step and added to the group, in a COPY of the CLI so
        # the repository's own step table is untouched.
        cli = (ROOT / "observatory.py").read_text(encoding="utf-8")
        patched = cli.replace(
            '    "test-queue": [PY, "tests/test_review_queue.py"],',
            '    "test-queue": [PY, "tests/test_review_queue.py"],\n'
            '    "purity-leak": [PY, "tests/_purity_leak.py"],')
        patched = patched.replace('"check": ["validate", "fabric",',
                                  '"check": ["purity-leak", "validate", "fabric",')
        alt = ROOT / "_observatory_purity_probe.py"
        alt.write_text(patched, encoding="utf-8")
        try:
            p = subprocess.run([PY, str(alt), "purity-only"], cwd=ROOT,
                               capture_output=True, text=True, timeout=300)
            # `purity-only` is not a group; the run should say so rather than
            # pretend. The real drive is the single step below.
            q = subprocess.run([PY, str(alt), "purity-leak"], cwd=ROOT,
                               capture_output=True, text=True, timeout=300)
            check("the planted suite runs", q.returncode == 0,
                  (q.stdout + q.stderr)[-200:])
            check("and it did write into the live scratch dir", probe.is_file(),
                  "the fixture must actually leak for this to prove anything")
        finally:
            alt.unlink(missing_ok=True)
        # Now the same leak inside a NON_MUTATING group, through the REAL CLI's
        # comparison: simulate by calling the two halves directly.
        import importlib.util
        spec = importlib.util.spec_from_file_location("obs_purity", ROOT / "observatory.py")
        obs = importlib.util.module_from_spec(spec)
        sys.modules["obs_purity"] = obs
        spec.loader.exec_module(obs)
        probe.unlink(missing_ok=True)
        before = obs.ignored_state()
        probe.write_text('{"leak": true}', encoding="utf-8")
        after = obs.ignored_state()
        touched = sorted(k for k in set(before[0]) | set(after[0])
                         if before[0].get(k) != after[0].get(k)
                         and k not in obs.IGNORED_WRITES_ALLOWED)
        # The scratch directory is the workspace's, outside the program tree,
        # so the engine names the file by its own path.
        check("the comparison names the file", touched == [str(probe)], str(touched))
        check("and the row counts are untouched by it", before[1] == after[1],
              f"{before[1]} vs {after[1]}")
    finally:
        probe.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def test_a_neighbours_turn_does_not_read_as_this_groups_mutation() -> None:
    """THE ROW-COUNT HALF of the foreign-write allowance, and it is DRIVEN.

    The companion's Stop hook writes a ledger row when ANY Claude session on this
    machine ends a turn. One ended mid-gate and the purity verdict
    reported the ledger count moving by one — a mutation the group under test
    had not made. The re-run was green, which is the tell: a guarantee that
    fails on a race teaches the reader to re-run rather than to look.

    The exclusion has to be NARROW or it hides the thing it was built beside, so
    both directions are planted here: the hook's own owner is invisible to the
    count, and any other owner still moves it.
    """
    import importlib.util, sqlite3
    spec = importlib.util.spec_from_file_location("obs_rows", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    sys.modules["obs_rows"] = obs
    spec.loader.exec_module(obs)

    check("the foreign row writer is named",
          getattr(obs, "FOREIGN_ROW_WRITER", "") == "agent:claude-code",
          str(getattr(obs, "FOREIGN_ROW_WRITER", None)))
    excluded = getattr(obs, "FOREIGN_ROWS_EXCLUDED", {})
    check("and exactly the two tables it writes are excluded",
          set(excluded) == {"ledger", "outbox"}, str(sorted(excluded)))
    check("every other watched table is counted whole",
          all(t not in excluded for t in obs.WATCHED_TABLES
              if t not in ("ledger", "outbox")), "")

    # DRIVEN, against a planted store rather than the live one.
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-rows-"))
    db = d / "planted.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE ledger (memory_id TEXT, owner TEXT);"
                      "CREATE TABLE outbox (memory_id TEXT);")
    con.commit()

    def counts() -> tuple[int, int]:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return (c.execute(excluded["ledger"], (obs.FOREIGN_ROW_WRITER,)).fetchone()[0],
                    c.execute(excluded["outbox"], (obs.FOREIGN_ROW_WRITER,)).fetchone()[0])
        finally:
            c.close()

    before = counts()
    con.execute("INSERT INTO ledger VALUES (?,?)", ("m1", obs.FOREIGN_ROW_WRITER))
    con.execute("INSERT INTO outbox VALUES (?)", ("m1",))
    con.commit()
    check("the hook's own row is invisible to the count", counts() == before,
          f"{before} -> {counts()}")

    con.execute("INSERT INTO ledger VALUES (?,?)", ("m2", "agent:observer"))
    con.execute("INSERT INTO outbox VALUES (?)", ("m2",))
    con.commit()
    check("any other owner still moves it",
          counts() == (before[0] + 1, before[1] + 1), f"{before} -> {counts()}")

    # An outbox row whose ledger entry is gone must be COUNTED, not assumed
    # foreign — an orphan is exactly what this check exists to notice.
    con.execute("INSERT INTO outbox VALUES (?)", ("orphan",))
    con.commit()
    check("an orphaned outbox row counts",
          counts()[1] == before[1] + 2, f"{before} -> {counts()}")
    con.close()


def test_the_verdict_names_who_moved_the_tree() -> None:
    """DRIVEN AS A DECISION, not by editing the repository's own files.

    The verdict has three cases and the third read as the first: with the status
    line set unchanged both `appeared` and `vanished` are empty, and
    `not vanished` named THE GATE — for a file another process rewrote in place.
    A tick rewriting `registry/findings.json` during a `check` therefore came out
    as the gate mutating the tree, which is the wrong-cause failure the
    vanished-case comment was written to prevent.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("obs_culprit", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    sys.modules["obs_culprit"] = obs
    spec.loader.exec_module(obs)

    check("a path appeared — that is the gate",
          obs.culprit_of([" M x.py"], [], ["x.py"]) == "the gate itself", "")
    check("dirt vanished — that is a commit landing mid-run",
          "DISAPPEARED" in obs.culprit_of([], [" M registry/x.json"], []), "")
    check("a registry file rewritten in place is NOT the gate",
          "no step of this group writes" in
          obs.culprit_of([], [], ["registry/findings.json"]),
          obs.culprit_of([], [], ["registry/findings.json"]))
    check("but a source file rewritten in place IS",
          obs.culprit_of([], [], ["tools/build_findings.py"]) == "the gate itself",
          obs.culprit_of([], [], ["tools/build_findings.py"]))
    check("and a mix of the two is the gate, not an excuse",
          obs.culprit_of([], [], ["registry/findings.json", "survey.py"])
          == "the gate itself",
          "one foreign path must not launder a real mutation beside it")
    check("nothing named at all falls back to the gate",
          obs.culprit_of([], [], []) == "the gate itself",
          "an unexplained digest change is not somebody else's by default")
    # CASE-INSENSITIVE: an assertion that fails on capitalisation chosen for
    # emphasis is testing the typography.
    src = (ROOT / "observatory.py").read_text(encoding="utf-8").lower()
    check("the paths it forgives are declared",
          obs.GROUP_WRITES_NOTHING_UNDER == ("registry/",),
          str(obs.GROUP_WRITES_NOTHING_UNDER))
    check("and the declaration carries its reason",
          "step of a non_mutating group writes" in src,
          "a path list that forgives a mutation needs the reason beside it")


def test_the_tracked_verdict_no_longer_returns_early() -> None:
    """The eighth instance of the class this suite exists to refuse. The
    tracked-tree branch `return 1`ed on the spot, so a tick landing mid-run
    skipped the ignored half AND the line saying the checks ran at all."""
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    check("the tracked change is recorded, not returned",
          "tree_dirty = True" in src, "")
    check("and the ignored half is still reached",
          src.index("tree_dirty = True") < src.index("if before_ignored is not None:"),
          "the flag must be set BEFORE the half git cannot see is checked")
    check("with the verdict decided after both",
          src.index("if before_ignored is not None:") < src.index("if tree_dirty:"),
          "")
    check("and the files that moved are named",
          "rewritten in place:" in src,
          "`git diff HEAD says which` told the reader to go and look")


def test_no_suite_imports_a_collector_that_runs_at_import() -> None:
    """Six collectors have no `if __name__ == "__main__"` guard, and three of
    them do their work at module level. `import merge` therefore performs a full
    merge against the selected estate — a suite once imported it, reloaded it,
    and ran two live merges from a test before its own output gave it away.

    The purity verdict catches the CONSEQUENCE — the write into `store/raw/` —
    but only after it has happened and only for a suite inside a checked group.
    This catches the cause, and it is derived rather than listed: a module with
    module-level calls and no `__main__` guard is one that runs when imported,
    whatever its name.
    """
    import ast
    executing = []
    for f in sorted((ROOT / "collectors").glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as exc:                                    # pragma: no cover
            check(f"{f.name} parses", False, str(exc))
            continue
        guarded = any(
            isinstance(n, ast.If) and ast.dump(n.test).find("__main__") != -1
            for n in tree.body)
        if guarded:
            continue
        # A module-level CALL statement is work done on import. An assignment
        # calling a function is the same thing, so both shapes count.
        #
        # EXCEPT `sys.path.insert`, which is the import shim every module in
        # this repository opens with and collects nothing. Counting it made
        # `credentials_registry` — a pure projection module with no `__main__`
        # at all — an "executing collector", and the rule then forbade the one
        # suite that drives it on fixtures. The rule's subject is a LIVE
        # COLLECTION at import; a path append is not one.
        def shim(node) -> bool:
            return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value.func) in ("sys.path.insert", "sys.path.append"))

        works = any(
            ((isinstance(n, ast.Expr) and isinstance(n.value, ast.Call))
             or (isinstance(n, (ast.Assign, ast.AnnAssign))
                 and isinstance(getattr(n, "value", None), ast.Call)
                 and not isinstance(n.value.func, ast.Attribute)))
            and not shim(n)
            for n in tree.body)
        if works:
            executing.append(f.stem)
    check("some collector runs at import, so this check has a subject",
          bool(executing), "if this fires, every collector is guarded and this "
                           "case can go")
    # THROUGH THE AST, not a substring. The first version searched for the text
    # `import <name>` and flagged THIS FILE, because the sentence explaining the
    # rule contains the words it forbids — an assertion matching what a file
    # says instead of what it does, written into the guard against that class. An `ast.Import` node is the thing that runs code.
    offenders = []
    for t in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(t.read_text(encoding="utf-8"))
        except SyntaxError:                                           # pragma: no cover
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in executing:
                    offenders.append(f"{t.name}:{node.lineno} imports {n}")
    check(f"no suite imports one of the {len(executing)} that do", not offenders,
          "; ".join(offenders[:4]) + " — read the source or drive the collector "
          "that owns the logic; an import here runs a live collection")


def test_the_page_is_allowed_and_says_why() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("obs_allow", ROOT / "observatory.py")
    obs = importlib.util.module_from_spec(spec)
    sys.modules["obs_allow"] = obs
    spec.loader.exec_module(obs)
    # THE PAGE AND ITS VERDICT, and nothing else. It was one path until the
    # smoke step began recording a verdict beside the page it executed.
    # One pair per split page since the split, and the property is unchanged: every
    # allowed write is a BUILT PAGE or the smoke verdict beside it, each under
    # `docs/`, each gitignored, each regenerated by the step that reads it. A
    # literal list of two would have to be retyped for every page; what the
    # assertion actually needs is the SHAPE, so that an allowance for anything
    # else — a registry file, a store artefact — is still refused.
    sys.path.insert(0, str(ROOT / "dashboard"))
    import shell
    expected = {"docs/projects-dashboard.html", "docs/projects-dashboard.smoke.json",
                f"docs/dashboard/{shell.ASSET_CSS}", f"docs/dashboard/{shell.ASSET_JS}"}
    for name in shell.NAMES:
        expected |= {f"docs/dashboard/{name}.html", f"docs/dashboard/{name}.smoke.json"}
    check("every allowed write is a built page or its smoke verdict, and nothing else",
          set(obs.IGNORED_WRITES_ALLOWED) == expected,
          f"unexpected: {sorted(set(obs.IGNORED_WRITES_ALLOWED) - expected)}; "
          f"missing: {sorted(expected - set(obs.IGNORED_WRITES_ALLOWED))}")
    why = obs.IGNORED_WRITES_ALLOWED["docs/projects-dashboard.html"]
    check("with a reason, not a bare entry", len(why.split()) >= 10, why)
    check("naming who reads it", "smoke" in why, why)
    check("and every allowed write carries one",
          all(len(v.split()) >= 10 for v in obs.IGNORED_WRITES_ALLOWED.values()),
          str({k: len(v.split()) for k, v in obs.IGNORED_WRITES_ALLOWED.items()}))
    # The ban that makes the container mean something: a write under store/raw/
    # is a fixture that forgot to redirect, every time, so the verdict lives
    # beside the page instead of being this ban's first exception.
    check("nothing under store/raw/ is an allowed write",
          not [k for k in obs.IGNORED_WRITES_ALLOWED if k.startswith("store/raw/")],
          str([k for k in obs.IGNORED_WRITES_ALLOWED if k.startswith("store/raw/")]))

    foreign = obs.FOREIGN_WRITES_IGNORED
    # The companion's fault log is written by the same hook in the same
    # sessions as its receipt, so it is exempted for exactly the reason the
    # receipt is — a SECOND file rather than a field in the first, because the
    # receipt is a slot the next session overwrites.
    #
    # The smoke verdict is NOT here: it is written by the gate's own `smoke`
    # step, so it is a step OUTPUT and belongs in the container above. Filing it
    # here would claim "not the gate wrote this" about a file the gate writes
    # every run — an exemption hiding the gate from itself.
    #
    # The always-on daemon rewrites its heartbeat receipt every few seconds for
    # as long as it runs — "concurrently with the gate" by construction. No step
    # of any group starts or stops the daemon, so exempting its receipt removes
    # no coverage.
    expected = {"store/raw/record-turn.json", "store/raw/integrity.json",
                "store/raw/tick-lease.json", "store/raw/store-faults.jsonl",
                "store/raw/companion-faults.jsonl", "store/raw/serverd.json"}
    check(f"exactly the {len(expected)} known foreign writers are exempted",
          set(foreign) == expected,
          f"unexpected: {sorted(set(foreign) - expected)}, "
          f"missing: {sorted(expected - set(foreign))}")
    check("and every one of them carries a reason, not a bare entry",
          all(len(v.split()) >= 8 for v in foreign.values()),
          str({k: len(v.split()) for k, v in foreign.items()}))
    fwhy = foreign.get("store/raw/record-turn.json", "")
    check("the companion's reason names the concurrency that makes it foreign",
          "concurrently" in fwhy and "session" in fwhy, fwhy[:120])
    check("the two containers do not overlap",
          not (set(obs.IGNORED_WRITES_ALLOWED) & set(foreign)),
          "one path cannot be both a step's output and a stranger's")


# ─────────── the four suites are clean ─────────────────────────────────

def test_no_suite_writes_into_the_live_scratch_directory() -> None:
    """The four that did, driven one at a time. Each redirected SOME of what a
    run touches and not the rest, which is why every one of them looked
    careful."""
    try:
        import concurrency
    except ImportError:
        # The discriminator module arrives with a parallel porting stream; until
        # then the attribution below cannot be made, and saying so beats a
        # verdict that cannot tell the tick from the suite.
        print("  SKIP  KNOWN-GAP: tests/concurrency.py is not in this distribution "
              "yet, so a suite's writes cannot be told from the tick's "
              "[gap: porting tests/concurrency.py closes it]")
        return
    for name in ("test_agent", "test_degradations", "test_plugins", "test_retention"):
        f = ROOT / "tests" / f"{name}.py"
        # The roster is four hardcoded names, so a renamed or deleted suite
        # made its purity check disappear while the gate stayed green. In the
        # portable runner only the suites of this distribution are present;
        # one that is absent is named rather than silently dropped.
        if not f.is_file():
            print(f"  SKIP  KNOWN-GAP: tests/{name}.py is not in this distribution, so "
                  f"nothing checked it for writing into the scratch directory "
                  f"[gap: porting tests/{name}.py closes it]")
            continue
        # THE TICK'S OWN CLOCK, around the same window. This suite hashed
        # `store/raw/*` before and after a run whose timeout is 1800s and named
        # the SUITE for anything that moved — while the scheduled tick writes
        # that directory every thirty minutes. So a tick landing inside the
        # window sent a reader looking for a redirect bug in a suite that has
        # none. The fourth instance of one class; `tests/concurrency.py` carries
        # the discriminator so a fifth reader need not rediscover it.
        mark_before = concurrency.tick_mark()
        before = scratch_hashes()
        subprocess.run([PY, str(f)], cwd=ROOT, capture_output=True, text=True,
                       timeout=1800)
        after = scratch_hashes()
        mark_after = concurrency.tick_mark()
        moved = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        guilty, why = concurrency.attribute(
            moved, concurrency.tick_ran(mark_before, mark_after), name)
        if moved and not guilty:
            # NOT A PASS. Nothing was proven about the suite, and printing this
            # as green would be the inversion this whole module exists to stop.
            print(f"  SKIP  {name}: {why} [covered: the attribution cases in "
                  f"tests/test_side_effect_attribution.py]")
            continue
        check(f"{name} leaves the live scratch dir alone", not guilty, why)


def test_the_agent_report_names_a_real_cause() -> None:
    """The consequence, checked where an operator would see it: the finding must
    not cite a path inside a temp directory."""
    f = paths.SCRATCH / "agent.json"
    if not f.is_file():
        print("  SKIP  no agent report on this machine "
              "[uncoverable: this asserts that the LIVE report cites no path "
              "inside a temp directory, so a fabricated report would assert "
              "the fabrication; the rule that writes `halted_by` is driven by "
              "tests/test_interpretation.py]")
        return
    doc = json.loads(f.read_text(encoding="utf-8"))
    halted = doc.get("halted_by") or ""
    check("the live report cites no test fixture",
          "observatory-loosekey" not in halted and "/var/folders/" not in halted,
          halted[:200])
    reg = json.loads((paths.REGISTRY / "findings.json").read_text(encoding="utf-8"))
    for row in reg.get("findings", []):
        if row["type"] == "interpretation.halted":
            check("and neither does the finding an operator reads",
                  "/var/folders/" not in row["detail"], row["detail"][:200])


if __name__ == "__main__":
    print("the gate's purity — enforced against the half git cannot see\n")
    for fn in (test_the_gate_watches_the_ignored_half,
               test_the_verdict_runs_even_when_a_step_fails,
               test_a_planted_leak_is_caught_and_named,
               test_a_neighbours_turn_does_not_read_as_this_groups_mutation,
               test_the_verdict_names_who_moved_the_tree,
               test_the_tracked_verdict_no_longer_returns_early,
               test_no_suite_imports_a_collector_that_runs_at_import,
               test_the_page_is_allowed_and_says_why,
               test_no_suite_writes_into_the_live_scratch_directory,
               test_the_agent_report_names_a_real_cause):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma gate that writes the report an operator reads is no longer green\033[0m")
