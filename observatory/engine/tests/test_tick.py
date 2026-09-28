#!/usr/bin/env python3
"""The scheduled tick as a WRITER: its identity, its lease, and its commit.

The registry is a guarded path. The scheduled tick once rewrote it every thirty
minutes holding no lease at all, and nothing ever committed the result — so the
tree was dirty within half an hour of any commit, the problem the wiki
projection had already solved for itself.

Three things have to hold, and only one of them is about git:

* **The tick has its own identity.** `agent_sync.run_id()` falls back to a
  SHARED entry for any process with no session — which is every launchd job and
  every plain shell command in this checkout. A lease taken under that identity
  is re-acquired by any shell and released by it too; it separates nothing.
* **A lost lease means the tick writes nothing.** Not "writes anyway and logs".
* **The commit is surgical.** Only paths under `registry/`, never a push, and
  never while an index or a merge is holding somebody's work in progress.

In the engine the registry's history lives in the private WORKSPACE, never in
the program checkout: the committer runs only when `features.registry_history`
is enabled and refuses a root that is not the workspace owning the configured
registry. Every fixture therefore initializes its own workspace in a temporary
directory and makes that workspace its own Git repository. Reading a real
checkout is what once made a hook test green for the wrong reason.
"""
from __future__ import annotations
import json, os, pathlib, re, subprocess, sys, tempfile
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
import tmp as tmpdir  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
TICK = ROOT / "tools/tick.sh"
LEASE = ROOT / "tools/tick_lease.py"
COMMIT = ROOT / "tools/commit_registry.py"
FAILURES: list[str] = []

# Every step that writes a file under registry/. The commit must run after all
# of them, in BOTH orchestrators, or it commits a registry it has not seen.
REGISTRY_WRITERS = {"emit", "export-ledger", "findings"}
WRITER_SCRIPTS = ("emit_registry.py", "export_ledger.py", "build_findings.py")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def git(*args: str, cwd: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()


#: The environment each fixture workspace runs under, keyed by its root.
ENVS: dict[str, dict] = {}
#: The registry file the fixtures edit. Any file under registry/ is committed;
#: this one is plain JSON the registry validator accepts in any content.
FIXTURE_FILE = "registry/sources.json"


def fake_repo(history: bool = True) -> pathlib.Path:
    """A throwaway private workspace that is its own Git repository.

    `history` sets `features.registry_history`, the switch without which the
    committer does nothing at all."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tick-")).resolve()
    home = d / "workspace"
    env = {k: v for k, v in os.environ.items()
           if k not in ("OBSERVATORY_REGISTRY", "OBSERVATORY_DB", "OBSERVATORY_STATE", "OBSERVATORY_SCRATCH")}
    env.update(OBSERVATORY_HOME=str(home), HOME=str(d))
    p = subprocess.run([PY, str(ROOT / "observatory.py"), "init"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError("fixture workspace failed to initialize: " + (p.stdout + p.stderr)[-300:])
    settings = home / "config/settings.json"
    doc = json.loads(settings.read_text(encoding="utf-8"))
    doc.setdefault("features", {})["registry_history"] = history
    settings.write_text(json.dumps(doc), encoding="utf-8")
    git("init", "-q", "-b", "main", cwd=home)
    git("config", "user.email", "t@example.test", cwd=home)
    git("config", "user.name", "Test", cwd=home)
    (home / "notes.txt").write_text("a file outside the registry\n")
    git("add", "-A", cwd=home)
    git("commit", "-q", "-m", "seed", cwd=home)
    ENVS[str(home)] = env
    return home


def edit_registry(repo: pathlib.Path, n: int) -> None:
    f = repo / FIXTURE_FILE
    doc = json.loads(f.read_text(encoding="utf-8"))
    doc.setdefault("sources", []).append({"id": f"SRC-9{n:03d}", "kind": "fixture",
                                          "description": f"synthetic edit {n}"})
    f.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def run_commit(repo: pathlib.Path, *args: str, root: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(COMMIT), "--root", str(root or repo), *args],
                          capture_output=True, text=True, timeout=120, env=ENVS[str(repo)])


def head(repo: pathlib.Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo)[1]


# -- identity ---------------------------------------------------------------

def test_the_tick_carries_its_own_identity() -> None:
    """Without an explicit id the tick IS every shell command in this checkout."""
    src = TICK.read_text(encoding="utf-8")
    check("tick.sh exports AGENT_SYNC_RUN_ID",
          re.search(r"^export AGENT_SYNC_RUN_ID=", src, re.M) is not None,
          "a lease under the shared identity separates nothing")

    sys.path.insert(0, str(ROOT / "tools"))
    sys.modules.pop("tick_lease", None)
    import tick_lease as tl
    mod = tl.agent_sync_module()
    if mod is None:
        print("  SKIP  agent-sync is not installed here")
        return
    got = mod.run_id.__wrapped__ if hasattr(mod.run_id, "__wrapped__") else mod.run_id
    os.environ["AGENT_SYNC_RUN_ID"] = tl.TICK_IDENTITY
    tick_rid = got(ROOT)
    os.environ["AGENT_SYNC_RUN_ID"] = "some-interactive-session"
    other_rid = got(ROOT)
    os.environ.pop("AGENT_SYNC_RUN_ID", None)
    check("the tick's run id differs from another run's", tick_rid != other_rid,
          f"{tick_rid} == {other_rid}")
    check("the tick's run id is stable across ticks",
          tick_rid == (os.environ.setdefault("AGENT_SYNC_RUN_ID", tl.TICK_IDENTITY) and got(ROOT)),
          "a fresh id per tick would strand the lease of a crashed one until TTL")
    os.environ.pop("AGENT_SYNC_RUN_ID", None)


def test_the_lease_helper_fails_closed_without_an_identity() -> None:
    """Falling back to the shared identity is the bug, so refuse instead."""
    env = {k: v for k, v in os.environ.items() if k != "AGENT_SYNC_RUN_ID"}
    p = subprocess.run([PY, str(LEASE), "acquire"], capture_output=True, text=True,
                       timeout=60, cwd=ROOT, env=env)
    check("acquire without AGENT_SYNC_RUN_ID exits non-zero", p.returncode != 0, p.stdout[:120])
    check("and says why", "identity" in (p.stdout + p.stderr).lower(), (p.stdout + p.stderr)[:120])


# -- the skip path ----------------------------------------------------------

# The one-off copy of this predicate lived here and was corrected twice in two
# minutes. It moved to `tests/tick_reader.py` when a fourth assertion broke on
# the same shape — a class seen twice becomes a script.
sys.path.insert(0, str(ROOT / "tests"))
from tick_reader import first_invocation                              # noqa: E402


def test_a_lost_lease_stops_the_tick_before_it_writes() -> None:
    src = TICK.read_text(encoding="utf-8")
    acquire_at = first_invocation(src, "tick_lease.py acquire")
    check("tick.sh acquires the lease", acquire_at != -1)
    for w in WRITER_SCRIPTS:
        at = first_invocation(src, w)
        check(f"the lease is taken before {w}", at == -1 or acquire_at < at,
              f"acquire at {acquire_at}, writer at {at}")
    m = re.search(r"tick_lease\.py acquire.*?\n(.*?)\nfi", src, re.S)
    body = m.group(1) if m else ""
    check("a lost lease exits the tick", "exit 0" in body,
          "the tick must not scan, emit or commit while another run may be writing")
    check("the lease is released on every path, including failure",
          re.search(r"^trap .*tick_lease\.py release", src, re.M) is not None,
          "without a trap a crashed tick holds the lease until its TTL")


def test_the_commit_runs_after_every_registry_writer() -> None:
    src = TICK.read_text(encoding="utf-8")
    at = src.find("commit_registry.py")
    check("tick.sh commits the registry", at != -1)
    for w in WRITER_SCRIPTS:
        wat = src.find(w)
        check(f"the commit runs after {w}", wat == -1 or wat < at, f"writer {wat}, commit {at}")

    sys.path.insert(0, str(ROOT))
    sys.modules.pop("observatory", None)
    import observatory as obs
    all_group = obs.GROUPS["all"]
    check("observatory.py has a commit-registry step", "commit-registry" in obs.STEPS)
    check("the `all` group commits the registry", "commit-registry" in all_group)
    if "commit-registry" in all_group:
        i = all_group.index("commit-registry")
        late = [s for s in all_group[i + 1:] if s in REGISTRY_WRITERS]
        check("no registry writer runs after the commit in `all`", not late, str(late))
    check("`check` does not commit anything", "commit-registry" not in obs.GROUPS["check"],
          "a gate that writes cannot be run to find out whether the tree is clean")


# -- the committer ----------------------------------------------------------

def test_it_commits_only_the_registry() -> None:
    repo = fake_repo()
    edit_registry(repo, 1)
    (repo / "notes.txt").write_text("an edit in progress\n")
    before = head(repo)
    p = run_commit(repo)
    check("it committed", head(repo) != before, p.stdout + p.stderr)
    _, out = git("status", "--porcelain", cwd=repo)
    check("work outside the registry is left alone", "notes.txt" in out, out)
    _, files = git("show", "--name-only", "--format=", "HEAD", cwd=repo)
    check("the commit holds only registry paths",
          all(f.startswith("registry/") for f in files.split()), files)


def test_it_refuses_over_a_staged_change() -> None:
    """`git commit` captures the whole index, so a staged file would be swept up."""
    repo = fake_repo()
    edit_registry(repo, 2)
    (repo / "notes.txt").write_text("staged by a person\n")
    git("add", "notes.txt", cwd=repo)
    before = head(repo)
    p = run_commit(repo)
    check("it refused", head(repo) == before, (p.stdout + p.stderr)[:160])
    check("it exits 0 — a refusal is a state, not a crash", p.returncode == 0, str(p.returncode))
    check("it names the staged work", "staged" in (p.stdout + p.stderr).lower(),
          (p.stdout + p.stderr)[:160])


def test_it_refuses_mid_merge() -> None:
    repo = fake_repo()
    (repo / ".git/MERGE_HEAD").write_text(head(repo) + "\n")
    edit_registry(repo, 3)
    before = head(repo)
    p = run_commit(repo)
    check("a merge in progress stops it", head(repo) == before, (p.stdout + p.stderr)[:160])


def test_dry_run_writes_nothing() -> None:
    repo = fake_repo()
    edit_registry(repo, 4)
    before = head(repo)
    p = run_commit(repo, "--dry-run")
    check("--dry-run leaves HEAD alone", head(repo) == before, p.stdout[:120])
    check("--dry-run says what it would do", "would commit" in p.stdout, p.stdout[:120])


def test_a_clean_registry_is_not_an_empty_commit() -> None:
    repo = fake_repo()
    before = head(repo)
    p = run_commit(repo)
    check("nothing to commit leaves HEAD alone", head(repo) == before, p.stdout[:120])
    check("and it exits 0", p.returncode == 0, str(p.returncode))


def test_history_is_off_unless_the_workspace_enables_it() -> None:
    """A scheduled writer of Git history is opt-in per workspace."""
    repo = fake_repo(history=False)
    edit_registry(repo, 5)
    before = head(repo)
    p = run_commit(repo)
    check("with registry_history off nothing is committed", head(repo) == before, p.stdout[:160])
    check("it exits 0 and names the switch",
          p.returncode == 0 and "registry_history" in p.stdout, (p.stdout + p.stderr)[:160])


def test_it_refuses_a_root_that_is_not_the_private_workspace() -> None:
    """Runtime facts must never be staged into the program's own history."""
    repo = fake_repo()
    edit_registry(repo, 6)
    before = head(repo)
    p = run_commit(repo, root=ROOT)
    check("the program checkout is refused as a history root",
          p.returncode == 1 and "NOT committing" in p.stderr, (p.stdout + p.stderr)[:200])
    other = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-tick-other-")).resolve()
    (other / "registry").mkdir()
    p = run_commit(repo, root=other)
    check("and so is a directory that does not hold this workspace's registry",
          p.returncode == 1 and "configured registry" in p.stderr, (p.stdout + p.stderr)[:200])
    check("and the workspace itself was not touched", head(repo) == before)


def test_it_never_pushes() -> None:
    src = COMMIT.read_text(encoding="utf-8")
    check("the word push appears in no git call",
          not re.search(r'git\([^)]*"push"', src) and '"push"' not in src,
          "what leaves the machine is the operator's decision")


def test_the_guard_is_asked_before_writing() -> None:
    """The committer is a writer of a guarded path like any other."""
    src = COMMIT.read_text(encoding="utf-8")
    check("it consults agent-sync's own guard", "guard" in src,
          "a writer that decides for itself whether it may write is not guarded")


if __name__ == "__main__":
    print("the tick as a writer — identity, lease, commit\n")
    for fn in (test_the_tick_carries_its_own_identity,
               test_the_lease_helper_fails_closed_without_an_identity,
               test_a_lost_lease_stops_the_tick_before_it_writes,
               test_the_commit_runs_after_every_registry_writer,
               test_it_commits_only_the_registry,
               test_it_refuses_over_a_staged_change,
               test_it_refuses_mid_merge,
               test_dry_run_writes_nothing,
               test_a_clean_registry_is_not_an_empty_commit,
               test_history_is_off_unless_the_workspace_enables_it,
               test_it_refuses_a_root_that_is_not_the_private_workspace,
               test_it_never_pushes,
               test_the_guard_is_asked_before_writing):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe tick writes under a lease and commits what it wrote\033[0m")
