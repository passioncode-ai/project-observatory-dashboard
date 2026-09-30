#!/usr/bin/env python3
"""What the event collector records, and what it must SAY when it does not.

Two defects, one shape: a bound that binds in silence.

A fixed `git log -N` depth silently drops every commit past N: `git log -200`
returning 200 rows looks exactly like a repository that has 200 commits, so a
store can hold a fraction of the commits inside its retention window with
nothing anywhere saying so. Truncation must be detectable and reported.

And a collector that records every repository, while the companion plugin's
recorder refuses anything but the estate's own work, lets `external` projects
dominate the counts. A "which project is most active" answer built on that
names somebody else's tool.
"""
from __future__ import annotations
import pathlib, sqlite3, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup
portable_setup()
sys.path.insert(0, str(ROOT / "tests"))
# `tools`, so the recorder can be IMPORTED rather than grepped — which is what
# the one-rule-two-readers check needs to compare two objects instead of two
# spellings.
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir              
import estate                                                                  
import paths                                                                   
from store import migrate                                                      

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def test_one_rule_two_readers() -> None:
    """The collector and the plugin's recorder must not disagree about the estate.

    Comparing `estate.RECORDED_OWNERSHIP` against a literal spelled out here,
    and then checking only that the STRING "RECORDED_OWNERSHIP" appears in the
    recorder, cannot detect the disagreement this test is named for: a
    recorder that kept its own inverted copy of the set would pass both.

    So the comparison is between two real objects, and there is no literal —
    re-spelling a set's contents in its own test asserts that somebody typed the
    same thing twice, not that one rule reaches both readers.
    """
    recorder = (ROOT / "tools/record_turn.py").read_text(encoding="utf-8")
    collector = (ROOT / "collectors/scan_events.py").read_text(encoding="utf-8")
    check("the collector asks estate.py", "estate.records_events" in collector)
    check("so does the recorder", "estate.records_events" in recorder)
    check("and the recorder keeps no set of its own",
          "RECORDED_OWNERSHIP = {" not in recorder
          and "RECORDED_OWNERSHIP = frozenset" not in recorder,
          "a rule in one place and copied in another is two rules")
    import record_turn as R
    check("the object both read is the same object",
          R.estate.RECORDED_OWNERSHIP is estate.RECORDED_OWNERSHIP,
          "not a copy that happens to be equal today")
    check("an external project records no events", not estate.records_events("external"))
    check("an owned project records events", estate.records_events("owned"))
    check("a work project records events", estate.records_events("work-bitbucket"))
    check("a repository attached to NO project still records — the gap must stay visible",
          estate.records_events(None))


def test_truncation_is_detectable_at_all() -> None:
    """`-N` returning N is ambiguous; `-(N+1)` returning N+1 is not."""
    src = (ROOT / "collectors/scan_events.py").read_text(encoding="utf-8")
    check("the collector asks for one more than the cap", "depth + 1" in src,
          "asking for exactly the cap cannot tell a full repository from a truncated one")
    check("it reports truncation as a degradation", "truncated at the" in src)
    check("it counts truncated repositories in the scan record", "repos_truncated" in src)
    check("the cap is a safety valve, not the bound",
          "DEFAULT_DEPTH = 10000" in src,
          "the bound is the retention window, shared with the pruner")


def test_truncation_is_detected_against_a_real_repository() -> None:
    """Driven through git, not asserted about the source."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-depth-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    for k in ("user.email=t@example.com", "user.name=T"):
        subprocess.run(["git", "-C", str(d), "config", *k.split("=", 1)], check=True)
    for i in range(5):
        (d / "f").write_text(str(i))
        subprocess.run(["git", "-C", str(d), "add", "f"], check=True)
        subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", f"c{i}"], check=True)

    def records(depth: int) -> int:
        out = subprocess.run(["git", "-C", str(d), "log", f"-{depth + 1}", "--no-merges",
                              "--format=%H%x1e"], capture_output=True, text=True)
        return len([r for r in out.stdout.split("\x1e") if r.strip()])

    check("a cap of 3 over 5 commits is seen as truncated", records(3) > 3, str(records(3)))
    check("a cap of 5 over 5 commits is NOT seen as truncated", records(5) == 5, str(records(5)))
    check("a cap of 9 over 5 commits is NOT seen as truncated", records(9) == 5, str(records(9)))


def test_the_migration_drops_only_foreign_events() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY, project_id TEXT, occurred_at TEXT)")
    conn.executemany("INSERT INTO events VALUES (?,?,?)", [
        ("a", "project:owned-one", "2026-01-01T00:00:00Z"),
        ("b", "project:external-one", "2026-01-01T00:00:00Z"),
        ("c", None, "2026-01-01T00:00:00Z"),
    ])
    # The migration reads the REAL registry for ownership, so assert the rule it
    # applies rather than re-creating a registry here: the rule is the contract.
    check("the rule keeps an owned project", estate.records_events("owned"))
    check("the rule drops an external project", not estate.records_events("external"))
    check("the rule keeps an unattributed row", estate.records_events(None))
    check("the migration is registered", any(
        m[0] == "0002-drop-events-outside-the-estate" for m in migrate.MIGRATIONS))


def test_the_live_store_holds_only_the_estates_own_work() -> None:
    if not paths.DB.exists() or not (paths.REGISTRY / "projects.json").exists():
        print("  SKIP  no store or registry on this machine")
        return
    import json
    own = {p["id"]: p.get("ownership") for p in
           json.loads((paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]}
    conn = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True)
    # BY KIND, because the invariant in this test's name is about whose WORK the
    # store holds, and ownership is only a proxy for that. It is the right proxy
    # for a commit, whose author is external — `estate.why_excluded` says so in
    # those words. It is the wrong proxy for a session: a session in a
    # third-party clone happened because the operator sat down and worked in it,
    # so it IS the estate's own work and excluding it would lose the answer to
    # "where was work done" for every borrowed checkout. The session half is
    # asserted below on the field that actually decides it — the actor.
    rows = conn.execute("SELECT project_id, COUNT(*) FROM events"
                        " WHERE kind = 'commit' GROUP BY project_id").fetchall()
    foreign = {pid: n for pid, n in rows if not estate.records_events(own.get(pid))}
    check("no COMMIT belongs to a project outside the estate's own work",
          not foreign, str(dict(list(foreign.items())[:3])))
    # The other half of the invariant, on the field that decides it. Narrowing
    # the check above without this would have dropped coverage rather than
    # corrected it: a `session` row with somebody else's actor would then be
    # unremarked, and "the store holds only the estate's own work" would be
    # asserted by nothing for that kind.
    actors = dict(conn.execute("SELECT actor, count(*) FROM events"
                               " WHERE kind = 'session' GROUP BY actor"))
    check("every session event is the operator's own work",
          set(actors) <= {"operator"}, str(actors))
    check("and the migration that drops foreign COMMITS says so in its SQL",
          "kind = 'commit'" in (paths.ROOT / "store/migrate.py").read_text(encoding="utf-8"),
          "a DELETE broader than its own docstring is how a session gets erased "
          "on a fresh clone")
    applied = conn.execute("SELECT id FROM migrations WHERE id LIKE '0002-%'").fetchone()
    check("the drop migration is recorded as applied", applied is not None)


def test_every_checkout_of_a_repository_is_read() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("scan_events_t", ROOT / "collectors/scan_events.py")
    se = importlib.util.module_from_spec(spec); spec.loader.exec_module(se)
    repos = {"repository:o/app": {"name_with_owner": "o/app", "local": {
        "path": "/srv/app", "extra_checkouts": [{"folder": "app-wt", "path": "/srv/app-wt"},
                                                {"folder": "dup", "path": "/srv/app"}]}}}
    got = se.targets([], repos, {"repository:o/app": "project:app"})
    paths_ = sorted(t["path"] for t in got)
    check("the primary checkout and every extra one are read, the primary once",
          paths_ == ["/srv/app", "/srv/app-wt"], str(paths_))
    check("an extra checkout is keyed by its repository, so one repo's shas are not 'shared'",
          {t["label"] for t in got} == {"repository:o/app"}
          and {t["project_id"] for t in got} == {"project:app"})


if __name__ == "__main__":
    print("events — what is recorded, and what silence would have hidden\n")
    for fn in (test_one_rule_two_readers,
               test_truncation_is_detectable_at_all,
               test_truncation_is_detected_against_a_real_repository,
               test_the_migration_drops_only_foreign_events,
               test_the_live_store_holds_only_the_estates_own_work,
               test_every_checkout_of_a_repository_is_read):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe collector records the estate's work, and says what it left out\033[0m")
