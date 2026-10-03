#!/usr/bin/env python3
"""The base collector, and what a missing `git` would have done to the registry.

`collectors/scan_filesystem.py` is where everything starts: `merge.py` reads its
output, and `scan_remotes.py` and `scan_bitbucket.py` derive what to probe from
it. Its `sh()` returned `""` for three different facts — a git command that
exited non-zero, one that timed out at 25 seconds, and one that could not run —
so a repository whose remote could not be READ was indistinguishable from one
that HAS no remote. That is the third instance of the class after the domain
probe and the transfer check, in the collector everything else is built on.

**What it would have cost, measured rather than reasoned.** Run the
scan with `git` off PATH, then run the merge against exactly that file (the
figures are that day's estate, and the ratio is what the experiment shows):

    with git      156 projects   172 repositories    11 local-folder anchors
    without git   206 projects   149 repositories   108 local-folder anchors
                                                    113 projects `local-only`

The emitter would have written all of it into the canonical registry and the
tick's `commit_registry` would have committed it, because **nothing anywhere
compared an emit against the one before it**.

Three fixes, at three depths:

1. `sh()` returns `(output, reason)`, and a failure that is an ANSWER is told
   from one that is not: `git remote get-url origin` exiting 2 with "No such
   remote" is a fact about the repository — two on the measured estate — and recording
   it as a degradation would put a permanent warning in front of the operator
   for a tree that is exactly as intended.
2. A PREFLIGHT: with `git` unrunnable and git folders present, the scan writes
   nothing and exits non-zero, so the last good `local.json` survives. A missing
   tool is one fact about the machine, not 99 claims about the estate.
3. The emitter refuses a swing over ±25% against the previous registry — past an
   absolute floor too, so a small estate gaining a project is not a "swing" —
   with `OBSERVATORY_ALLOW_BULK=1` for a genuine bulk change. That is the second
   line and it guards every cause rather than this one.

**And stopping became a result somebody can read.** `tick.sh`'s three early
exits — merge failed, emit failed or refused, validator red — each did `exit 0`
BEFORE the report was written, so `tick.json` kept the previous run's
`finished_at` and its empty `failed_steps`. A tick dying at `emit` for a week
showed a healthy report.

**Two of my own measurement instruments were wrong here**, and both are recorded
because the pattern repeats: `PATH="$SP/nogit:/usr/bin:/bin"` does not remove
git (it is in `/usr/bin`), so the first "git absent" run measured nothing; and
the regex extracting a shell function stopped at `^\\}` — which matched the `})`
line inside the Python heredoc — so the driver was truncated rather than the
function being broken.
"""
from __future__ import annotations
import json, os, pathlib, re, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402
import emitter_fixture

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


#: The one synthetic folder the scan must skip. The engine reads curation from
#: the workspace's `config/`, so the exclusion is declared there — in the
#: sandboxed workspace the runner built, never a real one.
EXCLUDED = "fixture-excluded"


def declare_exclusion() -> None:
    import paths
    f = paths.config_file("folder_exclusions.json")
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    names = [x for x in doc.get("names") or [] if x.get("name") != EXCLUDED]
    names.append({"name": EXCLUDED, "why": "This fixture is a container rather than a project."})
    doc["names"] = names
    doc.setdefault("prefixes", [])
    f.write_text(json.dumps(doc), encoding="utf-8")


def git_estate(root: pathlib.Path) -> pathlib.Path:
    """A projects root with one real, remoteless Git checkout and one excluded folder.

    Actual Git, locally, with no hooks, template, identity or remote lookup.
    """
    data = root / "data"
    data.mkdir(exist_ok=True)
    checkout = data / "fixture-local"
    checkout.mkdir(exist_ok=True)
    if not (checkout / ".git").exists():
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        subprocess.run(["git", "init", "--template=", str(checkout)], env=env,
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull,
                        "commit", "--allow-empty", "-m", "Synthetic fixture"], env=env,
                       check=True, capture_output=True)
    (data / EXCLUDED).mkdir(exist_ok=True)
    return data


def offline_bin(root: pathlib.Path) -> pathlib.Path:
    """A `gh` that refuses everything, first on PATH, so no step can reach a provider."""
    b = root / "offline-bin"
    b.mkdir(exist_ok=True)
    gh = b / "gh"
    gh.write_text("#!/bin/sh\necho 'offline fixture: no provider calls' >&2\nexit 1\n",
                  encoding="utf-8")
    gh.chmod(0o700)
    return b


def scan(dest: pathlib.Path, *, no_git: bool = False,
         data: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if data is None:
        fixture = dest.parent / 'fs-inputs'
        fixture.mkdir(exist_ok=True)
        data = git_estate(fixture)
    if no_git:
        # ONLY the empty directory. `"$empty:/usr/bin:/bin"` leaves git
        # reachable at /usr/bin/git, which is how the first version of this
        # measured a healthy run and called it a failure.
        empty = dest.parent / "nobin"
        empty.mkdir(exist_ok=True)
        env["PATH"] = str(empty)
    if data:
        env["OBSERVATORY_DATA"] = str(data)
    return subprocess.run([PY, "collectors/scan_filesystem.py", str(dest)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=900)


# ─────────── an answer is not a failure ────────────────────────────────

def test_a_repository_with_no_origin_is_a_fact_not_a_degradation() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fs-"))
    p = scan(d / "local.json")
    check("the scan succeeds", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    doc = json.loads((d / "local.json").read_text(encoding="utf-8"))
    check("the file is an object with folders", isinstance(doc, dict) and
          "folders" in doc, str(type(doc)))
    no_origin = [r["folder"] for r in doc["folders"] if r.get("no_origin")]
    check("a repository with no origin says so positively", no_origin,
          "an empty `remote` cannot distinguish itself from a timeout")
    check("and it is NOT in `degraded`",
          not [x for x in doc["degraded"] if any(f in x["source"] for f in no_origin)],
          "a permanent warning about an intended tree is how findings become noise")
    check("a healthy run degrades nothing at all", doc["degraded"] == [],
          str(doc["degraded"])[:200])


def test_the_probes_return_a_reason_they_could_not_run() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fsx", ROOT / "collectors/scan_filesystem.py")
    # The module scans at import time, so it is read rather than executed: the
    # behaviour is driven by the subprocess tests above and this asserts the
    # SHAPE of the return value, which is what made the three outcomes possible.
    src = (ROOT / "collectors/scan_filesystem.py").read_text(encoding="utf-8")
    check("sh returns a pair", 'return "", f"`{' in src and "exited {r.returncode}" in src,
          "a bare empty string cannot carry why it is empty")
    check("a timeout has its own reason", "did not finish in 25s" in src)
    check("and so does a tool that will not run", "could not run" in src)


# ─────────── the preflight ─────────────────────────────────────────────

def test_the_scan_refuses_when_git_cannot_run() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fsnogit-"))
    dest = d / "local.json"
    p = scan(dest, no_git=True)
    check("it exits non-zero", p.returncode != 0, f"exit {p.returncode}")
    check("and writes NOTHING", not dest.is_file(),
          "99 unread rows over a good file is worse than no write")
    check("naming the cause", "git" in p.stderr and "could not run" in p.stderr,
          p.stderr[-200:])
    check("counting the checkouts at risk", re.search(r"\d+ folder\(s\)", p.stderr)
          is not None, p.stderr[-200:])
    check("and quoting what it would have cost",
          "156 projects become 206" in p.stderr, p.stderr[-300:])


def test_an_unconfigured_projects_source_is_a_typed_refusal() -> None:
    """A new user who runs `full local` before `configure sources projects` met a
    FileNotFoundError traceback. The scan now says which source and which command."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fsnosrc-"))
    dest = d / "local.json"
    p = scan(dest, data=d / "unconfigured" / "projects")
    check("it exits 2", p.returncode == 2, f"exit {p.returncode}: {p.stderr[-200:]}")
    check("without a traceback", "Traceback" not in p.stderr, p.stderr[-300:])
    check("naming the configure command",
          "project-observatory full configure sources projects" in p.stderr, p.stderr[-300:])
    # The placeholder `<workspace>/unconfigured/projects` is not a folder anyone
    # chose: naming it as "the projects source" sent a new user looking for it.
    check("saying that no folder is configured, not naming the placeholder as one",
          "no projects folder is configured yet" in p.stderr and "unconfigured" not in p.stderr,
          p.stderr[-300:])
    check("and writing nothing", not dest.is_file())
    afile = d / "a-file"
    afile.write_text("x", encoding="utf-8")
    p = scan(dest, data=afile)
    check("a file where the folder should be is refused the same way",
          p.returncode == 2 and "Traceback" not in p.stderr and "not a directory" in p.stderr, p.stderr[-300:])


def test_a_refused_scan_leaves_the_previous_file_intact() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fskeep-"))
    dest = d / "local.json"
    dest.write_text(json.dumps({"folders": [{"folder": "kept"}], "degraded": []}),
                    encoding="utf-8")
    before = dest.read_bytes()
    scan(dest, no_git=True)
    check("byte for byte", dest.read_bytes() == before,
          "the refusal must not touch the last good answer")


# ─────────── the emitter's second line ─────────────────────────────────

def _emit(d: pathlib.Path, env: dict, **extra) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "collectors/emit_registry.py", str(d / "raw")], cwd=ROOT,
                          env=dict(env, **extra), capture_output=True, text=True, timeout=900)


def _set_projects(d: pathlib.Path, n: int) -> None:
    """Give the model exactly `n` projects, cloned from the fixture's own shape,
    so the emitter sees the one input it reads change and nothing else."""
    path = d / "raw/model.json"
    model = json.loads(path.read_text(encoding="utf-8"))
    # The seeded project with no repository is the template; it is kept beside
    # the model so a later call that has already emptied the model still has it.
    keep = d / "project-shape.json"
    if not keep.is_file():
        keep.write_text(json.dumps(model["projects"]["fixture-b"]), encoding="utf-8")
    shape = json.loads(keep.read_text(encoding="utf-8"))
    model["projects"] = {f"fixture-{i:02d}": dict(shape, name=f"Fixture {i:02d}")
                         for i in range(n)}
    path.write_text(json.dumps(model), encoding="utf-8")


def _registered(d: pathlib.Path) -> int:
    return len(json.loads((d / "registry/projects.json").read_text(encoding="utf-8"))["projects"])


def _bulk_estate(prefix: str, n: int) -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(tmpdir.mkdtemp(prefix=prefix))
    env = emitter_fixture.seed(d)
    env.pop("OBSERVATORY_ALLOW_BULK", None)
    _set_projects(d, n)
    p = _emit(d, env)
    check(f"baseline emit of {n} projects succeeds", p.returncode == 0, p.stderr[-300:])
    return d, env


def test_the_emitter_refuses_a_wholesale_swing() -> None:
    # A mass LOSS: 12 projects falling to 6 is the shape a collector failure
    # leaves, and it is past both the ratio and the absolute floor.
    d, env = _bulk_estate("observatory-bulk-", 12)
    _set_projects(d, 6)
    before = (d / "registry/projects.json").read_bytes()
    p = _emit(d, env)
    check("the emit is refused", p.returncode != 0, f"exit {p.returncode}")
    check("naming both counts and the limit",
          "would go from 12 to 6" in p.stderr and "limit ±25%" in p.stderr, p.stderr[-300:])
    check("the registry is untouched",
          (d / "registry/projects.json").read_bytes() == before)
    check("and the refusal names the override",
          "OBSERVATORY_ALLOW_BULK=1" in p.stderr, p.stderr[-160:])
    q = _emit(d, env, OBSERVATORY_ALLOW_BULK="1")
    check("the override proceeds", q.returncode == 0, (q.stdout + q.stderr)[-200:])
    check("and then the registry does change",
          (d / "registry/projects.json").read_bytes() != before,
          "an override that changes nothing is not an override")


def test_a_small_estate_grows_without_an_override() -> None:
    """A new user's fourth project. 3 → 4 is +33%, past the ratio, and was
    refused: the whole pipeline stopped at `emit` and the dashboard went stale on
    the day somebody added their first real folder. A handful of projects is
    ordinary work at any size, so growth is refused only past the ratio AND past
    an absolute floor."""
    d, env = _bulk_estate("observatory-bulk-grow-", 3)
    _set_projects(d, 4)
    p = _emit(d, env)
    check("3 → 4 projects emits without OBSERVATORY_ALLOW_BULK", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    check("and the fourth project is registered", _registered(d) == 4, str(_registered(d)))
    _set_projects(d, 9)
    p = _emit(d, env)
    check("4 → 9 (five more, at the floor) still emits", p.returncode == 0,
          (p.stdout + p.stderr)[-300:])
    _set_projects(d, 16)
    p = _emit(d, env)
    check("9 → 16 (seven more, past the floor and the ratio) is refused",
          p.returncode != 0 and "would go from 9 to 16" in p.stderr, (p.stdout + p.stderr)[-300:])
    check("and the refusal says how to accept a folder just added",
          "OBSERVATORY_ALLOW_BULK=1" in p.stderr and "added" in p.stderr, p.stderr[-400:])


def test_a_shrink_stays_strict() -> None:
    """Mass loss is the failure this guard exists for, so its floor is lower:
    one project gone is somebody archiving a folder, two or more past the ratio
    is refused, and an estate falling to nothing is refused at any size."""
    d, env = _bulk_estate("observatory-bulk-shrink-", 3)
    _set_projects(d, 2)
    p = _emit(d, env)
    check("3 → 2 (one project removed) emits", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    d, env = _bulk_estate("observatory-bulk-shrink4-", 4)
    _set_projects(d, 2)
    p = _emit(d, env)
    check("4 → 2 (two lost, −50%) is refused", p.returncode != 0, (p.stdout + p.stderr)[-300:])
    d, env = _bulk_estate("observatory-bulk-wipe-", 1)
    _set_projects(d, 0)
    p = _emit(d, env)
    check("1 → 0 (an estate emptied) is refused", p.returncode != 0 and "would go from 1 to 0" in p.stderr,
          (p.stdout + p.stderr)[-300:])


def test_an_ordinary_emit_is_not_refused() -> None:
    """The control: a guard that blocks normal work is worse than none."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-bulkok-"))
    env = emitter_fixture.seed(d)
    env.pop('OBSERVATORY_ALLOW_BULK', None)
    for _ in range(2):
        p = subprocess.run([PY, "collectors/emit_registry.py", str(d / "raw")],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        check("the unchanged fixture emits cleanly", p.returncode == 0,
              (p.stdout + p.stderr)[-300:])


# ─────────── stopping is a result ──────────────────────────────────────

def shell_function(name: str) -> str:
    """`^\\}$`, not `^\\}`. The Python heredoc inside `write_report` has a line
    beginning `})` at column 0, and the looser pattern cut the function in half
    — the driver was then a syntax error and looked like a defect in tick.sh."""
    src = (ROOT / "tools/tick.sh").read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.S | re.M)
    return m.group(0) if m else ""


def test_a_stopping_tick_records_why_it_stopped() -> None:
    for name in ("write_report", "bail"):
        check(f"`{name}` is a function in tick.sh", bool(shell_function(name)),
              "the report write was inline and unreachable from an early exit")
    if not shell_function("bail"):
        return
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-bail-"))
    driver = d / "drive.sh"
    driver.write_text(
        "#!/bin/bash\nset -uo pipefail\nlog(){ echo \"LOG: $1\"; }\n"
        f"PY={PY}\nFAILED_STEPS=\"\"\n"
        + shell_function("write_report") + "\n" + shell_function("bail") + "\n"
        'bail emit 1 "emit REFUSED a wholesale change"\n'
        'echo "REACHED THE LINE AFTER BAIL"\n', encoding="utf-8")
    p = subprocess.run(["bash", str(driver)], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_SCRATCH=str(d)),
                       capture_output=True, text=True, timeout=300)
    check("bail exits, so nothing after it runs",
          "REACHED THE LINE AFTER BAIL" not in p.stdout, p.stdout[-160:])
    check("it exits 0, because launchd must not retry a reported state",
          p.returncode == 0, str(p.returncode))
    f = d / "tick.json"
    check("and it leaves a report", f.is_file(),
          "the three most consequential outcomes wrote none")
    if f.is_file():
        doc = json.loads(f.read_text(encoding="utf-8"))
        check("naming the step that stopped it",
              doc["failed_steps"] == [{"step": "emit", "exit": 1}],
              str(doc["failed_steps"]))
        check("with a fresh finished_at", doc["finished_at"].startswith("20"),
              doc["finished_at"])


def test_all_three_early_exits_go_through_bail() -> None:
    src = (ROOT / "tools/tick.sh").read_text(encoding="utf-8")
    for what in ("bail merge", "bail emit", "bail validate"):
        check(f"`{what}` is wired", what in src,
              "an `exit 0` before the report is a tick nobody can diagnose")
    # ENUMERATED, with a reason each, rather than counted. The first version
    # asserted `len(lines) <= 3` and failed on two exits that are both correct:
    # a bound that happens to hold says nothing about why.
    EXEMPT = {
        'cd "$(dirname "$0")/.." || exit 0':
            "the script cannot find itself; there is no scratch dir to report into",
        '[ -x "$PY" ] || PY="$(command -v python3)" || exit 0':
            "no interpreter, so nothing could write a report anyway",
        "exit 0  # inside bail":
            "bail IS the reporter — it writes and then exits",
        "lease":
            "another run holds the registry. The tick correctly stands down and "
            "the next one is 30 minutes away; a `failed_steps` row here would "
            "raise a finding every time an operator session overlaps a tick",
    }
    body = shell_function("bail")
    unexplained = []
    for l in src.splitlines():
        if "exit 0" not in l or l.strip().startswith("#"):
            continue
        if "bail " in l or l in body:
            continue
        if l in EXEMPT:
            continue
        unexplained.append(l.strip())
    # The lease stand-down is matched by its surrounding log line rather than the
    # bare `exit 0`, which carries no words of its own.
    if unexplained == ["exit 0"] and "tick skipped — the registry belongs" in src:
        unexplained = []
    check("every early exit either reports or is exempt with a reason",
          not unexplained, str(unexplained))
    check("and the lease stand-down says why it is not a failure",
          "the next tick is 30 minutes away" in src,
          "an exit with no explanation reads as an oversight")


def test_the_step_failure_becomes_a_finding() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-bailfind-"))
    (d / "registry").mkdir()
    (d / "scratch").mkdir()
    (d / "scratch/tick.json").write_text(json.dumps({
        "finished_at": "2026-09-07T05:00:00Z",
        "failed_steps": [{"step": "emit", "exit": 1}]}), encoding="utf-8")
    p = subprocess.run([PY, "tools/build_findings.py", "--json"], cwd=ROOT,
                       env=dict(os.environ, OBSERVATORY_REGISTRY=str(d / "registry"),
                                OBSERVATORY_SCRATCH=str(d / "scratch"),
                                OBSERVATORY_DB=str(d / "absent.db")),
                       capture_output=True, text=True, timeout=600)
    try:
        found = [f for f in json.loads(p.stdout)["findings"]
                 if f["type"] == "tick.step_failed"]
    except (ValueError, KeyError):
        check("findings built", False, (p.stdout + p.stderr)[-300:])
        return
    check("the stopped tick is raised", len(found) == 1, str(len(found)))
    if found:
        check("naming the step", "emit" in found[0]["subject"], found[0]["subject"])


# ─────────── the exclusions moved to where the cost is ─────────────────

def test_the_scan_skips_what_the_merge_would_discard() -> None:
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fsx-"))
    p = scan(d / "local.json")
    doc = json.loads((d / "local.json").read_text(encoding="utf-8"))
    names = {x["folder"] for x in doc.get("skipped", [])}
    check('the declared synthetic exclusion is actually exercised',
          names == {EXCLUDED}, str(names))
    check("every skip carries its reason",
          all(x.get("why") for x in doc.get("skipped", [])),
          str(doc.get("skipped"))[:200])
    check("and skipped folders are absent from `folders`",
          not ({r["folder"] for r in doc["folders"]} & names), str(names))
    check("the run says how many it skipped", "skipped" in p.stdout, p.stdout[-160:])


def test_the_merged_model_is_unchanged_by_all_of_this() -> None:
    """Drive scan -> merge twice with identical synthetic local Git state."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fsm-"))
    (d / "raw").mkdir()
    env = dict(os.environ, OBSERVATORY_SCRATCH=str(d / "raw"),
               PATH=f"{offline_bin(d)}:{os.environ.get('PATH', '')}")
    data = git_estate(d)
    models = []
    for _ in range(2):
        p = scan(d/'raw/local.json', data=data)
        check('the synthetic scan runs', p.returncode == 0, p.stderr[-300:])
        if p.returncode:
            return
        p = subprocess.run([PY, 'collectors/merge.py', str(d/'raw')], cwd=ROOT,
                           env=env, capture_output=True, text=True, timeout=120)
        check('the real merge consumes the new scan shape', p.returncode == 0, p.stderr[-300:])
        if p.returncode:
            return
        models.append(json.loads((d/'raw/model.json').read_text()))
    for key in ('projects', 'repositories'):
        check('repeat scan preserves the ' + key + ' set',
              set(models[0][key]) == set(models[1][key]))
    local = models[1]['projects'].get('local-fixture-local', {})
    check('remoteless checkout survives as a local project', local.get('anchor') == 'local-folder')
    check('its measured commit date is retained', bool(local.get('last_activity')))
    check('excluded container creates no project',
          not any(p.get('name') == EXCLUDED for p in models[1]['projects'].values()))


def test_a_folder_on_an_uninventoried_host_records_its_unpushed_work() -> None:
    """One commit ahead of an upstream on a host the merge cannot key, with the
    remote unreachable now: the merge asks the tracking ref the clone already
    holds (no network, no fetch) and records `sync` and the count on
    `local_only`, so the clone rules can see it."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-fs-elsewhere-"))
    (d / "raw").mkdir()
    data = d / "data"
    data.mkdir()
    genv = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    bare, work = d / "upstream.git", data / "fixture-elsewhere"
    def g(*args, cwd=None):
        subprocess.run(["git", *(["-C", str(cwd)] if cwd else []), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid",
                        "-c", "core.hooksPath=" + os.devnull, *args],
                       env=genv, check=True, capture_output=True)
    g("init", "--bare", "--template=", "-b", "main", str(bare))
    g("init", "--template=", "-b", "main", str(work))
    g("commit", "--allow-empty", "-m", "published", cwd=work)
    g("remote", "add", "origin", str(bare), cwd=work)
    g("push", "-u", "origin", "main", cwd=work)
    g("commit", "--allow-empty", "-m", "only here", cwd=work)
    # The remote moves to a host nothing inventories and nothing can reach.
    g("remote", "set-url", "origin", "ssh://git@git.example.invalid/fixture-elsewhere.git", cwd=work)
    p = scan(d / "raw/local.json", data=data)
    check("the scan runs", p.returncode == 0, p.stderr[-300:])
    env = dict(os.environ, OBSERVATORY_SCRATCH=str(d / "raw"),
               PATH=f"{offline_bin(d)}:{os.environ.get('PATH', '')}")
    p = subprocess.run([PY, "collectors/merge.py", str(d / "raw")], cwd=ROOT,
                       env=env, capture_output=True, text=True, timeout=120)
    check("the merge runs", p.returncode == 0, p.stderr[-300:])
    if p.returncode:
        return
    model = json.loads((d / "raw/model.json").read_text())
    lo = (model["projects"].get("local-fixture-elsewhere") or {}).get("local_only") or {}
    check("it is a local-only project with a remote", lo and lo.get("unpublished") is False, str(lo)[:200])
    check("its state is recorded against the last fetch", lo.get("sync") == "ahead", str(lo)[:300])
    check("with the commit at stake counted", lo.get("unpushed") == 1, str(lo)[:300])
    check("and that it was compared with the last fetch, not the remote",
          lo.get("compared_with") == "last-fetch", str(lo)[:300])


def test_both_other_readers_use_the_shared_loader() -> None:
    for f in ("collectors/scan_remotes.py", "collectors/scan_bitbucket.py",
              "collectors/merge.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        check(f"{f} reads through local_scan", "local_scan.folders" in src,
              "three readers and one shape rule")
    sys.path.insert(0, str(ROOT / "collectors"))
    import local_scan
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-shape-"))
    (d / "old.json").write_text('[{"folder":"a"}]', encoding="utf-8")
    (d / "new.json").write_text('{"folders":[{"folder":"a"}],"degraded":[{"x":1}]}',
                                encoding="utf-8")
    check("the bare list still reads", len(local_scan.folders(d / "old.json")) == 1)
    check("the object reads too", len(local_scan.folders(d / "new.json")) == 1)
    check("the old shape reports no degradation rather than crashing",
          local_scan.degraded(d / "old.json") == [],
          "a file with no way to record one cannot be claimed to be clean OR broken")
    check("and the new shape's degradations are readable",
          len(local_scan.degraded(d / "new.json")) == 1)


if __name__ == "__main__":
    print("the base collector — and what a missing tool would have written\n")
    declare_exclusion()
    for fn in (test_a_repository_with_no_origin_is_a_fact_not_a_degradation,
               test_the_probes_return_a_reason_they_could_not_run,
               test_the_scan_refuses_when_git_cannot_run,
               test_a_refused_scan_leaves_the_previous_file_intact,
               test_an_unconfigured_projects_source_is_a_typed_refusal,
               test_the_emitter_refuses_a_wholesale_swing,
               test_a_small_estate_grows_without_an_override,
               test_a_shrink_stays_strict,
               test_an_ordinary_emit_is_not_refused,
               test_a_stopping_tick_records_why_it_stopped,
               test_all_three_early_exits_go_through_bail,
               test_the_step_failure_becomes_a_finding,
               test_the_scan_skips_what_the_merge_would_discard,
               test_the_merged_model_is_unchanged_by_all_of_this,
               test_a_folder_on_an_uninventoried_host_records_its_unpushed_work,
               test_both_other_readers_use_the_shared_loader):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma missing tool is one fact about the machine, not a new estate\033[0m")
