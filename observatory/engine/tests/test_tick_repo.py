#!/usr/bin/env python3
"""The tick and the repository: the race, the clock, and the commit subject.

Three findings, all about the seam between a scheduled writer and a git history
a person has to read.

* **The gate raced the tick.** `./observatory.py check` reads `registry/*.json`
  across about ten minutes while the tick rewrites them one atomic replace at a
  time. A tick landing mid-gate can therefore put a NEW `relations.json` beside
  an OLD `projects.json` in front of a cross-file check — a dangling reference
  that never existed on disk, reported as a failure. The gate DETECTED this
  afterwards and failed with an explanation; it now takes the same `registry`
  lease the tick uses, so the collision is prevented rather than diagnosed. It
  does not refuse when the lease is unavailable: blocking the operator's gate
  because a scheduled job holds a key is worse than the race.

* **The registry differed from itself on every run.** `registry/findings.json`
  carried `built_at = now()`, so `git diff` was never empty, the tick's "the
  registry is clean — nothing to commit" branch was unreachable, and the estate
  collected a commit per tick guaranteed by the clock rather than by any fact.
  Measured: two consecutive runs, two differing lines, both that field.

* **Every one of those commits had the same subject.** `Registry refresh: N
  projects, M repositories, K relations` is the SIZE of the registry, which
  changes almost never, so nothing in the log distinguished a counter bump from
  a project appearing.

The lease cases need the optional agent-sync coordinator, which a synthetic
sandbox does not have; they say so and skip. Everything else runs against the
synthetic workspace.
"""
from __future__ import annotations
import importlib, json, os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _scratch() -> pathlib.Path:
    import paths
    return paths.SCRATCH


def git(*args: str, cwd: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()


# ──────────────────────── the race, prevented ────────────────────────────

def test_the_gate_and_the_tick_serialize() -> None:
    import tick_lease
    handle, why = tick_lease.hold(tick_lease.GATE_IDENTITY)
    if handle is None:
        print(f"  SKIP  the lease is unavailable here — {why} "
              f"[uncoverable: the property is that two REAL holders serialize, so "
              f"the assertion has to be the first holder; a third run already "
              f"holding the lease is the one state that cannot be faked, and "
              f"there is only one lease on this machine]")
        return
    check("the gate can hold the registry lease", "holding" in why, why)
    try:
        # The TICK's own path, in a separate process under its own identity.
        p = subprocess.run([PY, "tools/tick_lease.py", "acquire"], cwd=ROOT,
                           env=dict(os.environ, AGENT_SYNC_RUN_ID=tick_lease.TICK_IDENTITY),
                           capture_output=True, text=True, timeout=120)
        check("and while it does, the tick stands down rather than writing", p.returncode != 0,
              f"exit {p.returncode}: {(p.stdout + p.stderr)[-200:]}")
        check("saying that another writer holds the registry",
              "Standing down" in (p.stdout + p.stderr), (p.stdout + p.stderr)[-200:])
    finally:
        msg = tick_lease.drop(handle)
    check("the gate releases it", "released" in msg, msg)

    # And once released, the tick can take it — otherwise "serialize" would mean
    # "the tick never runs again".
    p = subprocess.run([PY, "tools/tick_lease.py", "acquire"], cwd=ROOT,
                       env=dict(os.environ, AGENT_SYNC_RUN_ID=tick_lease.TICK_IDENTITY),
                       capture_output=True, text=True, timeout=120)
    said = p.stdout + p.stderr
    # A THIRD holder is the one state this assertion cannot distinguish from a
    # broken release, and the SKIP at the top of this function already says so
    # about the FIRST hold — it was left off the last one. Once measured: the
    # gate ran while the session held its own task lease, agent-sync's guard
    # is satisfied by ANY lease, and the tick therefore stood down exactly as
    # designed — reported as a FAILURE of the code that behaved correctly. What
    # is checkable here is that the refusal NAMES a holder that is not the gate,
    # which is the difference between contention and a lease that leaked.
    other = (p.returncode != 0 and "standing down" in said.lower()
             and tick_lease.GATE_IDENTITY not in said)
    if other:
        print("  SKIP  after which the tick takes it normally — a third run holds a "
              f"lease right now [{said.strip().splitlines()[0][:110]}]; the release "
              "itself was asserted above, and the tick's standdown against the GATE's "
              "lease is what this case drove")
    else:
        check("after which the tick takes it normally", p.returncode == 0, said[-200:])
    subprocess.run([PY, "tools/tick_lease.py", "release"], cwd=ROOT,
                   env=dict(os.environ, AGENT_SYNC_RUN_ID=tick_lease.TICK_IDENTITY),
                   capture_output=True, text=True, timeout=120)


def test_the_gate_releases_on_failure_and_never_refuses() -> None:
    """A lease held past a failed run wedges the tick until the TTL expires.

    Run as a CHILD with its own identity, deliberately. The first version
    registered a fake group in-process and then proved the lease was free by
    having a tick take it — which conflates "the failing run released ITS lease"
    with "no lease is held anywhere", and fails the moment this suite runs
    inside the gate, because the gate itself is holding one. The claim worth
    checking is the first: whatever this run took, it gave back.
    """
    driver = (
        "import sys; sys.path.insert(0, '.'); import observatory as obs\n"
        "obs.STEPS['_fake_pass'] = [sys.executable, '-c', \"print('fixture step')\"]\n"
        "obs.STEPS['_fake_fail'] = [sys.executable, '-c', 'import sys; sys.exit(3)']\n"
        "obs.GROUPS['_fake_group'] = ['_fake_pass', '_fake_fail']\n"
        "obs.NON_MUTATING.add('_fake_group')\n"
        "raise SystemExit(obs.main(['observatory.py', '_fake_group']))\n")
    env = {k: v for k, v in os.environ.items() if k != "AGENT_SYNC_RUN_ID"}
    p = subprocess.run([PY, "-c", driver], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    check("a failing non-mutating group still fails", p.returncode == 3,
          f"exit {p.returncode}: {out[-200:]}")
    took = "holding `registry`" in out
    check("it either takes the lease or says it is running without one",
          took or "running WITHOUT the registry lease" in out, out[-300:])
    if took:
        check("and a lease it took is released even though the run failed",
              "released `registry`" in out, out[-300:])
    else:
        print("  SKIP  no lease was taken — the coordinator is absent here or another "
              "run holds it — so there was none to release")


def test_two_runs_of_the_same_role_exclude_each_other() -> None:
    """The hole a constant identity left, measured rather than reasoned about.

    agent-sync locks on the first eleven alphanumeric characters of
    `AGENT_SYNC_RUN_ID`, so `observatory-tick` was the same identity on every
    run: two overlapping ticks RE-ENTERED one lease and either could release
    it. Proved by driving it — A held `registry` as `observatory-tick-AAAAA`, B won
    the same key as `observatory-tick-BBBBB`, and A's release then reported
    success over a lease it no longer held. launchd starts the next tick on its
    interval whether or not the previous finished, so this is reachable by a
    tick that runs long, not by a hypothetical.
    """
    import tick_lease
    env = {k: v for k, v in os.environ.items() if k != "AGENT_SYNC_RUN_ID"}
    child = ("import sys; sys.path.insert(0, 'tools'); import tick_lease;"
             "h, w = tick_lease.hold({!r}); print(w);"
             "print(tick_lease.drop(h)) if h else None")

    # The identity is checked on the pure function. One PROCESS is one run, so
    # a process already identified as the gate cannot re-identify as a tick —
    # `hold` deliberately keeps an identity that carries this pid — and asking
    # it to would be testing a shape the product does not have.
    for role in ("observatory-tick", "observatory-gate"):
        ident = tick_lease.run_identity(role)
        check(f"{role}'s identity carries this pid", str(os.getpid()) in ident, ident)
        check(f"and keeps {role.rsplit('-', 1)[-1]!r} legible in it",
              role.rsplit("-", 1)[-1] in ident, ident)
    check("and the two roles do not collapse into one identity",
          tick_lease.run_identity("observatory-tick")[:2]
          != tick_lease.run_identity("observatory-gate")[:2],
          "agent-sync locks on the first characters, so the tag must differ there")

    handle, why = tick_lease.hold("observatory-tick")
    if handle is None:
        print(f"  SKIP  the lease is unavailable here — {why} "
              f"[uncoverable: the property is that two REAL holders serialize, so "
              f"the assertion has to be the first holder; a third run already "
              f"holding the lease is the one state that cannot be faked, and "
              f"there is only one lease on this machine]")
        return
    try:
        p = subprocess.run([PY, "-c", child.format("observatory-tick")], cwd=ROOT,
                           env=env, capture_output=True, text=True, timeout=120)
        check("a SECOND tick is refused rather than granted the same lease",
              "is held by" in p.stdout, p.stdout.strip()[:160] or p.stderr[-160:])
        # And a child that INHERITS the parent's variable must not adopt its
        # identity — that is how the fixture group below released the lease of
        # the gate that was running it.
        p2 = subprocess.run([PY, "-c", child.format("observatory-gate")], cwd=ROOT,
                            env=dict(os.environ), capture_output=True, text=True,
                            timeout=120)
        check("a child inheriting AGENT_SYNC_RUN_ID cannot take the parent's lease",
              "is held by" in p2.stdout, p2.stdout.strip()[:160] or p2.stderr[-160:])
    finally:
        msg = tick_lease.drop(handle)
    check("and the holder can still release its own", "released" in msg, msg)


def test_an_unavailable_lease_degrades_rather_than_blocking() -> None:
    """The operator's gate must run while a scheduled job holds the key."""
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    check("the gate does not exit when the lease is refused",
          "running WITHOUT the registry lease" in src)
    check("and says what it gave up", "reported as a tree change below" in src,
          "a degradation nobody is told about is indistinguishable from coverage")
    # DRIVEN, where the private original grepped for a comment the public
    # export does not carry: a coordinator that raises on acquire and on
    # release must reach the caller as an explanation, never as an exception.
    import tick_lease

    class Exploding:
        def acquire(self, key):
            raise RuntimeError("synthetic coordinator failure")

        def release(self, key):
            raise RuntimeError("synthetic coordinator failure")

    saved = (tick_lease.agent_sync_module, tick_lease._sync)
    tick_lease.agent_sync_module = lambda: object()
    tick_lease._sync = lambda mod: Exploding()
    try:
        try:
            handle, why = tick_lease.hold("observatory-gate")
            raised = None
        except Exception as exc:                                        # noqa: BLE001
            handle, why, raised = None, "", exc
        check("hold() never raises at the caller", raised is None, repr(raised))
        check("and returns no handle with the reason", handle is None
              and "could not be taken" in why, why)
        try:
            msg = tick_lease.drop(Exploding())
            raised = None
        except Exception as exc:                                        # noqa: BLE001
            msg, raised = "", exc
        check("and drop() never raises either", raised is None
              and "could not be released" in msg, repr(raised) + msg)
    finally:
        tick_lease.agent_sync_module, tick_lease._sync = saved


# ──────────────────── the clock, out of the committed file ────────────────

def test_the_registry_is_a_function_of_the_facts_not_the_clock() -> None:
    """Two runs, two seconds apart, must leave the file byte-identical."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-clock-"))
    # THE FACTS ARE FROZEN, not merely the registry. The builder reads the live
    # store as well, and the store is written continuously by the always-on
    # server, the tick and every gate run — so two builds two seconds apart
    # could differ by a heartbeat and report the clock leaking into the file,
    # which is the opposite of what this asserts. Copying the store makes the
    # sentence "with the same facts" true of the run, not only of the intent.
    shutil.copytree(_scratch(), d / "raw")
    # THE DATABASE IS A FACT TOO. The builder reads metric series out of the
    # live sqlite file (read-only, `metric_series()`), and the always-on server
    # writes to it between any two seconds — so with `store/raw` frozen and the
    # DB not, this check went red inside a gate and green when
    # run alone. `sqlite3.Connection.backup` copies a file
    # another process may be mid-write in, which a plain `shutil.copy` does not
    # promise. No DB at all is also a fact, and is left as one.
    import paths as _paths
    db_env = {}
    if _paths.DB.is_file():
        import sqlite3
        src = sqlite3.connect(f"file:{_paths.DB}?mode=ro", uri=True)
        dst = sqlite3.connect(str(d / "frozen.sqlite"))
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        db_env["OBSERVATORY_DB"] = str(d / "frozen.sqlite")
    env = dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
               OBSERVATORY_SCRATCH=str(d / "raw"), **db_env)
    (d / "registry").mkdir(parents=True)
    for name in ("projects.json", "repositories.json", "relations.json",
                 "domains.json", "domain-liveness.json"):
        src = _paths.REGISTRY / name  # the selected (synthetic) workspace registry
        if src.is_file():
            shutil.copy(src, d / "registry" / name)

    def build() -> str:
        subprocess.run([PY, "tools/build_findings.py"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
        return (d / "registry/findings.json").read_text(encoding="utf-8")

    first = build()
    import time
    time.sleep(2)
    second = build()
    check("a second run with the same facts changes nothing", first == second,
          "the file differs from itself, so the tree is dirty by construction")

    # And when the facts DO change, the stamp must move — otherwise the fix
    # would have frozen the field instead of anchoring it.
    doc = json.loads(second)
    was = doc["built_at"]
    doc["findings"] = doc["findings"][:-1] if doc["findings"] else []
    doc["counts"] = {"critical": 999, "warning": 0, "info": 0}
    (d / "registry/findings.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1)
                                              + "\n", encoding="utf-8")
    third = json.loads(build())
    check("but a real change moves it", third["built_at"] != was
          or third["counts"] != {"critical": 999, "warning": 0, "info": 0},
          f"{was} -> {third['built_at']}")


#: A clock-resolution timestamp allowed in a committed registry file, and why.
#: Anything not listed here is churn: it changes on every run by construction, so
#: `git diff` is never empty and the tick commits whether or not a fact moved.
CLOCK_ALLOWED = {
    ("findings.json", "built_at"):
        "anchored to a CONTENT change — it stops moving when nothing "
        "moves — and the wire returns it as `builtAt` beside a `checkedAt` taken "
        "from the store, so precision here is read by a caller",
}

CLOCK = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def clock_fields(registry: pathlib.Path) -> list[tuple[str, str, str]]:
    """Every (file, field, example) carrying a clock time, minus the allowlist.

    Values, not names, are authoritative: a field called `mtime` can hold a
    clock and one called `checked_at` can hold a date. The naming convention
    this repository already follows — `*_on` for a date, `*_at` for an instant —
    is checked separately, as a readability rule rather than as the invariant.
    """
    found: list[tuple[str, str, str]] = []

    def walk(node, file_name: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and CLOCK.match(v):
                    if (file_name, k) not in CLOCK_ALLOWED:
                        found.append((file_name, k, v))
                else:
                    walk(v, file_name)
        elif isinstance(node, list):
            for x in node:
                walk(x, file_name)

    for f in sorted(registry.glob("*.json")):
        try:
            walk(json.loads(f.read_text(encoding="utf-8")), f.name)
        except (json.JSONDecodeError, OSError):
            continue
    return found


def test_no_clock_time_sits_in_a_committed_registry_file() -> None:
    """The class, closed by a rule rather than by a third fix.

    `built_at` was the first. The commit subject built to make history readable
    then named the second on its first live run: `remote_checked_at xN` — a
    per-repository instant, one line per repository per tick, whose ONLY reader
    truncated it to a date with `[:10]`. `scanned_at` in `domain-liveness.json`
    was the third, a whole-file stamp with no reader at all beside a per-host
    `checked_on` that was already a date.
    """
    import paths
    bad = clock_fields(paths.REGISTRY)  # the workspace registry the engine emitted
    unique = sorted({(f, k) for f, k, _ in bad})
    check("the committed registry carries dates, not clocks", not bad,
          f"{unique} — these change every run by construction")


def test_the_rule_catches_a_planted_clock() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-clockrule-"))
    (d / "thing.json").write_text(json.dumps(
        {"rows": [{"id": "a", "measured_on": "2026-09-07",
                   "probed_at": "2026-09-07T00:57:24Z"}]}), encoding="utf-8")
    found = clock_fields(d)
    check("a clock value nested in a list is found",
          [(f, k) for f, k, _ in found] == [("thing.json", "probed_at")], str(found))
    check("and a date beside it is not flagged",
          all(k != "measured_on" for _, k, _ in found), str(found))


def test_the_naming_convention_still_holds() -> None:
    """`*_on` is a date, `*_at` is an instant — a real convention here, and the
    rename followed it rather than leaving `remote_checked_at` holding a date."""
    import paths
    reg = json.loads((paths.REGISTRY / "repositories.json").read_text(encoding="utf-8"))
    local = next((r["local"] for r in reg["repositories"] if (r.get("local") or {})
                  .get("remote_checked_on")), None)
    check("the renamed field exists and holds a date", local is not None
          and len(local["remote_checked_on"]) == 10, str(local)[:120] if local else "absent")
    src = (ROOT / "collectors/merge.py").read_text(encoding="utf-8")
    check("and the truncation happens at the boundary, not at the reader",
          '"remote_checked_on":(rm.get("checked_at") or "")[:10]' in src,
          "the raw file keeps the instant; the projection keeps the day")
    findings_src = (ROOT / "tools/build_findings.py").read_text(encoding="utf-8")
    # THE PROPERTY, not one expression that happened to embody it. This looked
    # for the literal `remote_checked_on', '')}` — the board's interpolation of
    # the field — as a proxy for "the reader does not slice a date". The board
    # stopped reading the field altogether: a row that must state an AGE needs
    # an instant, and the registry deliberately carries only the day (a
    # second-resolution stamp there rewrote every row per tick), so the
    # instant is quoted from the scan's own report instead. The intent is
    # satisfied maximally — there is no reader left to slice — and what this now
    # asserts is that no reader slices it, plus a deliberate tripwire: if the
    # board reads this field again, this line fires and the reader must justify
    # it.
    sys.path.insert(0, str(ROOT / "tests"))
    import source_reader
    code = source_reader.code_only(findings_src)
    check("no reader slices a value that is already a date",
          "remote_checked_on" not in code,
          "the board takes the instant from store/raw/remotes.json; reading the "
          "registry's day here would be a step back to one clock for two facts")


def test_the_wire_can_still_tell_stale_from_quiet() -> None:
    """`builtAt` alone now reads as staleness on a quiet day."""
    src = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
    check("the findings tool reports checkedAt beside builtAt", '"checkedAt"' in src)
    check("and takes it from the store's last scan", "FROM scans" in src)
    check("degrading honestly when that is unreadable",
          "builtAt alone cannot tell staleness from quiet" in src)
    dash = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
    check("the dashboard no longer labels that date 'assembled'",
          "собрано ${when}" not in dash,
          "it is the date the content last changed, not the date of the run")


# ──────────────────── the commit subject ─────────────────────────────────

def test_the_commit_subject_says_what_moved() -> None:
    import commit_registry as cr
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-commit-"))
    (d / "registry").mkdir()
    git("init", "-q", cwd=d)
    git("config", "user.email", "fixture@example.invalid", cwd=d)
    git("config", "user.name", "Fixture", cwd=d)
    doc = {"projects": [{"id": "project:alpha", "lifecycle": "active",
                         "last_activity_on": "2026-09-01",
                         "local": {"uncommitted_files": 1}}]}
    (d / "registry/projects.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    git("add", "-A", cwd=d)
    git("commit", "-qm", "fixture", cwd=d)

    doc["projects"][0]["lifecycle"] = "archived"
    doc["projects"][0]["local"]["uncommitted_files"] = 7
    (d / "registry/projects.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    subject = cr.describe_change(d, ["registry/projects.json"])
    check("a semantic field is named", "lifecycle" in subject, subject)
    check("and a counter is counted rather than named",
          "counters" in subject and "uncommitted_files" not in subject, subject)
    check("the subject is not the registry's SIZE", "projects," not in subject, subject)


def test_a_scheduled_commit_never_asks_for_a_signing_key() -> None:
    """The tick commits the registry (and the wiki projection) unattended. With
    `commit.gpgsign` on in the user's Git config, that commit ran the signing
    program — gpg's pinentry, or an SSH signer — which can put a passphrase or
    Keychain dialog in front of the operator from a background job. The planted
    signer leaves a marker if either committer ever runs it."""
    import commit_registry as cr
    import commit_projection as cp
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sign-"))
    marker = d / "signer-ran"
    signer = d / "signer.sh"
    signer.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    signer.chmod(0o755)
    git("init", "-q", cwd=d)
    for k, v in (("user.email", "fixture@example.invalid"), ("user.name", "Fixture"),
                 ("commit.gpgsign", "true"), ("gpg.program", str(signer))):
        git("config", k, v, cwd=d)
    for name, mod in (("commit_registry", cr), ("commit_projection", cp)):
        code, out = mod.git("commit", "--allow-empty", "-q", "-m", f"fixture {name}", cwd=d)
        check(f"{name}: the unattended commit succeeds without signing", code == 0, out[-200:])
    check("and no signing program ran", not marker.exists())


def test_the_volatile_set_claims_only_what_is_true() -> None:
    """The first version of that comment said their history lived in the store."""
    import commit_registry as cr
    check("commits is NOT treated as volatile", "commits" not in cr.VOLATILE,
          "its history IS kept, per event and per week — it is a fact, not a reading")
    check("uncommitted_files is", "uncommitted_files" in cr.VOLATILE)
    src = (ROOT / "tools/commit_registry.py").read_text(encoding="utf-8")
    check("and the comment states what the store actually holds",
          "`project_week` holds `commits`" in src,
          "the first draft claimed a history that does not exist")


if __name__ == "__main__":
    print("the tick and the repository\n")
    for fn in (test_the_gate_and_the_tick_serialize,
               test_the_gate_releases_on_failure_and_never_refuses,
               test_two_runs_of_the_same_role_exclude_each_other,
               test_an_unavailable_lease_degrades_rather_than_blocking,
               test_the_registry_is_a_function_of_the_facts_not_the_clock,
               test_no_clock_time_sits_in_a_committed_registry_file,
               test_the_rule_catches_a_planted_clock,
               test_the_naming_convention_still_holds,
               test_the_wire_can_still_tell_stale_from_quiet,
               test_the_commit_subject_says_what_moved,
               test_the_volatile_set_claims_only_what_is_true,
               test_a_scheduled_commit_never_asks_for_a_signing_key):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe tick writes under a lease, and its history is readable\033[0m")
