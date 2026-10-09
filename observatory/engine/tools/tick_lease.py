#!/usr/bin/env python3
"""The scheduled tick's coordination boundary: one identity, one key, one answer.

The tick rewrites `registry/*.json` on a fixed interval. Those are guarded
paths, so the tick must hold a lease while writing them; otherwise it is the
very second writer the lease exists to exclude.

Two facts about the coordination tool shape everything here:

* **A process with no session shares one identity with every other.**
  The run id is resolved from the session id, then from an ancestor the
  session-start hook stamped, then from a SHARED fallback entry. A scheduled
  job reaches the fallback, so a lease taken there is re-acquired by any plain
  shell command in this checkout and released by it too. `AGENT_SYNC_RUN_ID` is
  the documented override and the only way the tick becomes a distinct writer.
  Without it this helper REFUSES rather than taking a lease that separates
  nothing.

* **`guard()` does not compare the lease key with the path.** It asks whether
  the path is guarded and whether this run holds ANY lease. So two runs holding
  two different keys may both write the registry and the tool will allow both.
  Mutual exclusion on the registry is therefore this repository's convention,
  not the tool's guarantee: **every writer of `registry/*.json` takes the key
  `registry`**, and because a careless session may take a task id instead, the
  tick also stands down when any other run holds anything at all. A skipped tick
  costs nothing (the next one is one interval away) and a torn registry costs
  far more.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, pathlib, sys, subprocess, fcntl, signal, time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths
import configuration

#: The key every writer of `registry/*.json` takes. See the module docstring:
#: agent-sync arbitrates per key, so a shared name is what makes it exclusive.
REGISTRY_KEY = "registry"

#: The tick's identity, stable across runs on purpose. A fresh id per tick would
#: strand the lease of a crashed one until its 45-minute TTL; ticks never overlap
#: because launchd will not start a second copy of a label that is still running.
TICK_IDENTITY = "observatory-tick"

_PLUGIN = pathlib.Path.home() / ".claude/plugins/cache/agent-sync/agent-sync"


def _version_key(name: str) -> tuple:
    """Sort 1.20.0 above 1.9.0. Lexicographic order gets that backwards, and the
    consequence is silently running an old copy of the coordination tool."""
    parts = []
    for chunk in name.split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, 0, chunk))
    return tuple(parts)


def agent_sync_script() -> pathlib.Path | None:
    if not _PLUGIN.is_dir():
        return None
    for v in sorted(_PLUGIN.iterdir(), key=lambda p: _version_key(p.name), reverse=True):
        s = v / "skills/agent-sync/scripts/agent_sync.py"
        if s.exists():
            return s
    return None


def agent_sync_module():
    """The script imported as a module, or None. Importing rather than shelling
    out is what gives access to `all_holdings()` — the CLI reports holdings only
    as prose, and parsing prose to decide whether to write is worse than not
    checking at all."""
    if not (paths.HOME / ".claude/agent-sync.json").is_file():
        return None
    script = agent_sync_script()
    if script is None:
        return None
    try:
        spec = importlib.util.spec_from_file_location("agent_sync_mod", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:                                                           
        return None


def _sync(mod):
    """A Sync bound to this repository, or None if this version's shape moved."""
    try:
        cwd = os.getcwd()
        os.chdir(paths.HOME)
        try:
            return mod.Sync()
        finally:
            os.chdir(cwd)
    except Exception:
        return None


def other_holders(mod) -> tuple[list[tuple[str, str]] | None, str]:
    """[(run, key)] held by runs that are not this one, or (None, reason).

    None is not an empty list: it means the question could not be asked, and the
    caller must degrade out loud rather than read silence as an all-clear."""
    s = _sync(mod)
    if s is None:
        return None, "this agent-sync version does not expose its holdings"
    try:
        return ([(str(h.get("run")), k) for k, h in sorted(s.all_holdings().items())
                 if h.get("run") and h["run"] != s.rid], "")
    except Exception as exc:
        return None, f"holdings could not be read: {type(exc).__name__}"


#: The GATE's identity, distinct from the tick's on purpose. Both write nothing
#: to the registry — the gate reads it for ten minutes — but they must be
#: distinguishable in the journal, or "who was holding this when the tick stood
#: down" has no answer.
GATE_IDENTITY = "observatory-gate"


def run_identity(role: str) -> str:
    """A lease identity that is unique per RUN, with the role still readable.

    The coordination tool derives the identity it locks on from only a short
    alphanumeric prefix of the run id. Two processes whose ids share that
    prefix therefore RE-ENTER one lease rather than excluding each other, and
    one may later report a successful release over a lease it no longer holds.

    A constant role name is thus exactly the wrong identity for mutual
    exclusion. Distinct roles differ inside the prefix and exclude each other
    correctly, but two runs of the SAME role do not, and the scheduler starts
    the next tick on its interval whether or not the previous one has finished.
    A tick that overran its interval would have had two concurrent registry
    writers with a lease between them that excluded nothing.

    So the varying part goes FIRST and the role rides along behind it: a short
    role tag plus the process id, then the role, which is unique per process
    and still legible in the journal.
    """
    # The tag comes from the LAST word of the role, not the first characters of
    # the whole string: roles sharing a common prefix would otherwise collapse
    # into one identity, reintroducing the very failure this function exists to
    # remove.
    tag = role.rsplit("-", 1)[-1][:2]
    return f"{tag}{os.getpid()}-{role}"


def hold(identity: str) -> tuple[object | None, str]:
    """Take the registry lease IN-PROCESS, for a reader that must not be
    interleaved with a writer. Returns (handle, explanation); a None handle is
    never fatal — the caller decides, and the gate proceeds unleased and says so.

    Why a reader needs it at all: `project-observatory full check` reads `registry/*.json`
    across about ten minutes, and the tick rewrites those files one atomic
    replace at a time. A tick landing mid-gate therefore lets a cross-file check
    read the NEW `relations.json` against the OLD `projects.json` and report a
    dangling reference that never existed on disk — a false red — or pass over a
    combination that never existed either. The gate already DETECTS the collision
    after the fact (`tree_state()` in observatory.py) and fails with an
    explanation; a lease turns a detected collision into a prevented one.

    The identity is set here rather than inherited, and that matters: a lease
    taken under one identity and used by another reads as a FOREIGN holder to the
    process doing the work. Exporting `AGENT_SYNC_RUN_ID` in a subshell to
    acquire, then editing from a session whose own run id differs, blocked an
    edit on 2026-09-07 with the holder being me."""
    # NOT `setdefault`: a child inherits this variable from its parent's
    # environment, so a nested run would adopt the PARENT's identity, re-enter
    # the lease the parent is holding, and release it on the way out — leaving
    # the parent believing it still held the registry. That is precisely what
    # the fixture group inside `tests/test_tick_repo.py` did to the gate running
    # it (observed 2026-09-07: `released \`registry\`` in the middle of a gate
    # that had another twelve steps to go).
     
    # An identity that does not carry THIS process's pid names somebody else's
    # run, so it is replaced rather than honoured. A value that does carry it —
    # `tools/tick.sh` exports one for its acquire and its release trap — is left
    # exactly as the caller set it.
    want = run_identity(identity)
    current = os.environ.get("AGENT_SYNC_RUN_ID") or ""
    if f"{os.getpid()}-" not in current:
        os.environ["AGENT_SYNC_RUN_ID"] = want
    mod = agent_sync_module()
    if mod is None:
        return None, "agent-sync is not installed here"
    s = _sync(mod)
    if s is None:
        return None, "agent-sync is installed but its shape has moved"
    try:
        won, holder = s.acquire(REGISTRY_KEY)
    except Exception as exc:                                                      
        return None, f"the lease could not be taken: {type(exc).__name__}: {exc}"
    if not won:
        return None, f"`{REGISTRY_KEY}` is held by {holder or 'another run'}"
    return s, f"holding `{REGISTRY_KEY}` as {os.environ.get('AGENT_SYNC_RUN_ID')}"


def drop(handle) -> str:
    """Release on every path, including failure. Never raises, for the reason
    `release()` gives: a cleanup that fails loudly teaches people to ignore logs."""
    if handle is None:
        return ""
    try:
        return (f"released `{REGISTRY_KEY}`" if handle.release(REGISTRY_KEY)
                else f"`{REGISTRY_KEY}` was not ours to release")
    except Exception as exc:
        return f"the lease could not be released cleanly: {type(exc).__name__}: {exc}"


#: Outcomes that mean the tick DID NOT RUN this cycle. `unguarded` is not one of
#: them — agent-sync being absent lets the tick proceed, and counting that as a
#: skip would report a gap that never happened.
SKIPPED = ("stood-down", "refused")


def record_outcome(outcome: str, *, holder: str = "", reason: str = "") -> dict:
    """Leave a receipt saying whether this cycle ran, and how many have not.

    A tick that stands down correctly (because another writer holds the
    registry) still leaves the estate on stale data while every surface looks
    normal, and a message in the tick log is read by nothing on a schedule. The
    receipt makes the skip visible.

    `consecutive_skips` is carried in the receipt itself rather than kept
    anywhere else: the count IS the fact a reader wants, and a counter in a
    second place is a second thing to keep true. `last_acquired_at` survives a
    skip for the same reason: it is what says how stale the estate is, and a
    skip that erased it would answer "how old is this?" with silence.
    """
    # THE ROOT FIRST. This file runs as `python tools/tick_lease.py` from the
    # tick's shell, so `sys.path[0]` is `tools/` and neither `paths` nor
    # `atomic` is importable until the root is added. Importing `paths` before
    # that raises inside `acquire()`; tests/test_tick_repo.py covers the tick
    # still taking the lease after the gate releases it.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import paths
    f = paths.SCRATCH / "tick-lease.json"
    prior: dict = {}
    if f.is_file():
        try:
            prior = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # An unreadable receipt is replaced. It holds no canon — only the
            # last look at a lease — and refusing to write would lose the
            # outcome this call exists to record.
            prior = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skipped = outcome in SKIPPED
    doc = {
        "at": now, "outcome": outcome, "holder": holder, "reason": reason,
        "consecutive_skips": (int(prior.get("consecutive_skips") or 0) + 1
                              if skipped else 0),
        "last_acquired_at": (prior.get("last_acquired_at") if skipped
                             else now) or prior.get("last_acquired_at"),
    }
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        import atomic
        atomic.write_json(f, doc)
    except Exception as exc:                                                      
        # NAMED, never fatal. The tick must not fail because its own receipt
        # could not be written — but a receipt that vanishes silently is the
        # defect this function was added to close, one layer in.
        print(f"the tick-lease receipt could not be written: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return doc


def acquire() -> int:
    if not os.environ.get("AGENT_SYNC_RUN_ID"):
        print("REFUSING to take a lease: no AGENT_SYNC_RUN_ID, so this process would share "
              "one identity with every shell command in this checkout. Export "
              f"AGENT_SYNC_RUN_ID={TICK_IDENTITY} before calling.", file=sys.stderr)
        record_outcome("refused", reason="no AGENT_SYNC_RUN_ID")
        return 1
    mod = agent_sync_module()
    if mod is None:
        print("optional agent-sync is not configured; scheduled ticks use the workspace supervisor lock")
        record_outcome("unguarded", reason="no optional agent-sync; scheduled tick has independent workspace lock")
        return 0
    s = _sync(mod)
    if s is None:
        print("agent-sync is installed but its shape has moved; the tick stands down rather "
              "than writing the registry on an assumption.", file=sys.stderr)
        record_outcome("stood-down", reason="agent-sync's shape has moved")
        return 1
    won, holder = s.acquire(REGISTRY_KEY)
    if not won:
        print(f"lost `{REGISTRY_KEY}` — held by {holder or 'another run'}. Standing down: "
              "another writer has the registry.", file=sys.stderr)
        record_outcome("stood-down", holder=holder or "another run",
                       reason="another writer has the registry")
        return 1

    others, why = other_holders(mod)
    if others is None:
        print(f"holding `{REGISTRY_KEY}`, but could not check for other runs — {why}. "
              "Proceeding: the key itself is exclusive against anyone following the "
              "convention.")
        record_outcome("acquired", reason=f"could not check for other runs — {why}")
        return 0
    if others:
        run, key = others[0]
        s.release(REGISTRY_KEY)
        print(f"standing down: run {run} holds `{key}`. agent-sync's guard is satisfied by "
              "ANY lease, so that run may write the registry too — and a skipped tick is "
              "cheaper than two writers.", file=sys.stderr)
        record_outcome("stood-down", holder=run,
                       reason=f"run {run} holds `{key}`")
        return 1
    print(f"holding `{REGISTRY_KEY}` as {TICK_IDENTITY}; no other run holds anything")
    record_outcome("acquired")
    return 0


def release() -> int:
    """Always exit 0: this runs from a trap, and a cleanup path that fails loudly
    over a lease it never held teaches the next reader to ignore the log."""
    mod = agent_sync_module()
    s = _sync(mod) if mod else None
    if s is None:
        return 0
    print(f"released `{REGISTRY_KEY}`" if s.release(REGISTRY_KEY)
          else f"`{REGISTRY_KEY}` was not ours to release")
    return 0


def status() -> int:
    mod = agent_sync_module()
    if mod is None:
        print("agent-sync is not installed here")
        return 0
    others, why = other_holders(mod)
    if others is None:
        print(f"holdings unreadable — {why}")
        return 0
    print("\n".join(f"  {run}  holds  {key}" for run, key in others) if others
          else "  no other run holds anything")
    return 0


FEATURE_STEPS = {
    "agent": "agent", "index": "embeddings", "notify": "notifications",
    "sweep": "fixture_cleanup", "retention": "retention",
    "project-into-vault": "wiki_projection", "projection": "wiki_projection",
    "scrub-companion": "companion_remediation", "registry": "registry_history",
    # The machine survey (processes, memory, disk, worktrees, branches) and the
    # cleanup that acts on it. `cleanup` runs its auto tier only when
    # features.auto_cleanup is ALSO true; with it off, the step writes the plan.
    "machine": "machine_watch", "git-hygiene": "machine_watch", "cleanup": "machine_watch",
    "lifecycle": "machine_watch",
}


INTEGRATION_STEPS = {
    "scan-gh": "github", "scan-vault": "wiki", "scan-sessions": "sessions",
    "remotes": "git_remotes", "bitbucket": "bitbucket", "domains": "domains",
    "heroku": "heroku", "openrouter": "openrouter", "scan-cloudflare": "cloudflare",
    "scan-mcp": "mcp", "remote-env": "remote_env", "google": "google",
    # Not a collector, but it reads only what the sessions collector writes
    # (store/raw/sessions.json): with sessions off it failed every tick (0.19.3).
    "lost": "sessions",
}


# region tick-watchdog — docs: docs/runs/2026-10-03-lifecycle-contract/README.md#f5-f6-every-step-is-bounded
#: Exit status of a step or tick the watchdog stopped, GNU `timeout`'s convention.
TIMED_OUT = 124
#: Seconds a step's process group gets between SIGTERM and SIGKILL.
STEP_GRACE = 5
#: Seconds the supervisor gives the whole tick group between SIGTERM and SIGKILL.
#: The tick plist's ExitTimeOut sits above SUPERVISOR_GRACE + STEP_GRACE, so
#: launchd never SIGKILLs the supervisor while it is still stopping its children.
SUPERVISOR_GRACE = 10
#: A step's own wall-clock limit. The slowest steps measured on a loaded machine
#: (2026-10-03, last 30 ticks): leaks 849 s, git-hygiene 418 s, scan-fs 374 s.
DEFAULT_STEP_SECONDS = 900
STEP_SECONDS = {"leaks": 1200}
#: The whole tick's ceiling, below the 30-minute interval (lifecycle LC-03: a
#: watchdog shorter than the interval). `install_launchd.build` lowers it for a
#: shorter interval and writes it into the plist.
DEFAULT_CEILING_SECONDS = 1500
CEILING_ENV = "OBSERVATORY_TICK_CEILING_SECONDS"
DEADLINE_ENV = "OBSERVATORY_TICK_DEADLINE"
#: The wall-clock moment (epoch seconds) a step's own watchdog fires, handed to
#: the step so it can stop in time and write what it has instead of being killed
#: with nothing written (OBS-40). `step_budget` reads it.
STEP_DEADLINE_ENV = "OBSERVATORY_STEP_DEADLINE"
#: THE TAIL: every step from `merge` on — what turns the collectors' facts into
#: the registry, the findings, the dashboard and the notice a person reads.
#: Measured 2026-10-07: 14 ticks in four days spent their whole ceiling on
#: `integrity` and `scan-fs` under a starved disk, so the tail never ran and the
#: board stayed as it was without a word. A step NOT named here is a collector
#: and stops `tail_reserve_seconds()` before the ceiling. An unknown step falls
#: on the collectors' side on purpose: a forgotten collector would otherwise eat
#: the tail's time. `test_every_step_after_the_merge_is_a_tail_step` derives the
#: set from `tick.sh` and fails when the two disagree.
TAIL_STEPS = frozenset({
    "merge", "emit", "validate", "events", "snapshot", "diff", "plugins", "rollup",
    "agent", "index", "retention", "sweep", "corroborate", "ledger", "lost", "findings",
    "dashboard", "smoke", "findings-recheck", "notify", "registry", "project-into-vault",
    "projection", "links", "logs",
})
#: THE TAIL'S OWN SPLIT (0.20.1). Its optional steps — plugins, the agent's model calls,
#: the vector index, retention and the review machinery — ran before the findings and the
#: dashboard and could still use the last of the time: 14 ticks in a row on 2026-10-08/09
#: reached the ceiling inside `plugins` (142 s) and `agent`, and the board stood still for
#: 13 hours. An optional step leaves the core `core_reserve_seconds()`; skipped, its output
#: is the previous tick's, which the core reads as it reads any older receipt.
CORE_TAIL_STEPS = frozenset({
    "merge", "emit", "validate", "events", "snapshot", "diff", "findings", "dashboard",
    "smoke", "findings-recheck", "notify", "registry", "project-into-vault", "projection",
    "links", "logs",
})
SOFT_TAIL_STEPS = TAIL_STEPS - CORE_TAIL_STEPS
#: The tail took 106 s on a quiet machine (2026-10-07, `rollup` to `tick done`);
#: 300 s leaves it room under load. A fifth of the ceiling for a short interval.
TAIL_RESERVE_SECONDS = 300
#: From `findings` to `tick done`: 20 s on a quiet machine (2026-10-07); 120 s under load.
CORE_RESERVE_SECONDS = 120


def ceiling_seconds() -> float:
    raw = os.environ.get(CEILING_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_CEILING_SECONDS)
    return value if 1 <= value <= 3600 else float(DEFAULT_CEILING_SECONDS)


def tail_reserve_seconds() -> float:
    """Seconds before the tick's ceiling that collectors leave to the tail."""
    return min(float(TAIL_RESERVE_SECONDS), ceiling_seconds() / 5)


def core_reserve_seconds() -> float:
    """Seconds before the ceiling that the tail's optional steps leave to its core."""
    return min(float(CORE_RESERVE_SECONDS), ceiling_seconds() / 12)


def _step_marker() -> pathlib.Path:
    """Where the running step's process group is written, for the supervisor's sweep."""
    return paths.STATE / "tick-step.pgid"


def _stop_group(pgid: int, grace: float) -> None:
    """SIGTERM the group, SIGKILL whatever is left after `grace` seconds."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 1.0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)


def bounded(name: str, command: list[str], *, limit: float | None = None,
            grace: float = STEP_GRACE) -> int:
    """Run one tick step in a process group of its own, under a wall-clock watchdog.

    The step gets the smaller of its own limit and the time left before the
    tick's ceiling (`OBSERVATORY_TICK_DEADLINE`, set by the supervisor); with no
    time left it does not start. On the limit, or when this runner is told to
    stop, the step's WHOLE group is stopped — a collector's `git` or `du`
    included — and the exit is TIMED_OUT (or 128 + the signal). The group id is
    left in `tick-step.pgid` while the step runs, so a supervisor that had to
    SIGKILL this runner can still reach the group (`supervised`).

    A collector — any step not in TAIL_STEPS — also leaves the tail its reserve,
    and every step is told the moment its watchdog fires (`STEP_DEADLINE_ENV`)."""
    limit = float(STEP_SECONDS.get(name, DEFAULT_STEP_SECONDS) if limit is None else limit)
    reserve = (0.0 if name in CORE_TAIL_STEPS else core_reserve_seconds() if name in SOFT_TAIL_STEPS
               else tail_reserve_seconds())
    deadline = os.environ.get(DEADLINE_ENV, "").strip()
    if deadline:
        try:
            limit = min(limit, float(deadline) - reserve - time.time())
        except ValueError:
            pass
    if limit <= 0:
        print("not started — the tick's ceiling has been reached" if not reserve else
              f"not started — the last {reserve:.0f} s before the tick's ceiling are kept for "
              f"the registry, the findings and the dashboard; the previous run's output stands", flush=True)
        return TIMED_OUT
    marker = _step_marker()
    env = {**os.environ, STEP_DEADLINE_ENV: f"{time.time() + limit:.0f}"}
    child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(str(child.pid), encoding="utf-8")
    except OSError:
        pass                     # the sweep is a second line of defence, not the first
    stopped: list[int] = []

    def stop(signum, frame):
        stopped.append(signum)
        raise InterruptedError(signum)
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        return child.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        print(f"exceeded its {limit:.0f} s limit — the step's process group was stopped", flush=True)
        return TIMED_OUT
    except InterruptedError:
        return 128 + stopped[-1]
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if child.poll() is None or stopped:
            _stop_group(child.pid, grace)
            child.wait()
        else:
            # The leader is done; anything it left in its group goes with it.
            _stop_group(child.pid, min(grace, 1.0))
        try:
            if marker.read_text(encoding="utf-8").strip() == str(child.pid):
                marker.unlink()
        except OSError:
            pass


def record_run(started: str, outcome: str, code: int | None, reason: str) -> None:
    """`store/raw/tick-run.json`: start, end, outcome, reason — one per run (LC-03).

    Written by the supervisor, which outlives the tick script, so a tick stopped
    at its ceiling or by a signal still leaves a record; `tick.json` is the
    script's own and is missing exactly then."""
    doc = {"started_at": started, "ended_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "outcome": outcome, "exit": code, "reason": reason}
    try:
        import atomic
        paths.SCRATCH.mkdir(parents=True, exist_ok=True)
        atomic.write_json(paths.SCRATCH / "tick-run.json", doc)
        import tick_health
        tick_health.record(paths.SCRATCH, doc)
    except Exception as exc:                                                      # noqa: BLE001
        print(f"the tick-run receipt could not be written: {type(exc).__name__}: {exc}", file=sys.stderr)
# endregion tick-watchdog


def step_allowed(name: str) -> bool:
    feature = FEATURE_STEPS.get(name)
    integration = INTEGRATION_STEPS.get(name)
    return ((feature is None or configuration.enabled(feature, "features"))
            and (integration is None or configuration.enabled(integration)))


def supervised(command: list[str]) -> int:
    """One writer per workspace, independent of optional agent-sync installation."""
    configuration.validate_workspace(required=True)
    if not configuration.enabled("scheduler", "features"):
        print("tick disabled: enable features.scheduler in this workspace first")
        return 0
    if not command:
        raise ValueError("A supervised command is required")
    paths.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if paths.STATE.is_symlink():
        raise configuration.ConfigurationError("State directory cannot be a symbolic link")
    paths.STATE.chmod(0o700)
    fd = os.open(paths.STATE / "tick.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("tick skipped: another run holds this workspace's lock")
            return 0
        ceiling = ceiling_seconds()
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        env = {**os.environ, "OBSERVATORY_HOME": str(paths.HOME),
               "OBSERVATORY_TICK_SUPERVISOR_PID": str(os.getpid()),
               DEADLINE_ENV: f"{time.time() + ceiling:.0f}"}
        _step_marker().unlink(missing_ok=True)
        child = subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)
        interrupted = []
        def stop(signum, frame):
            interrupted.append(signum)
            raise InterruptedError("Tick supervisor received a stop signal")
        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        outcome, code, reason = "failed", None, ""
        try:
            code = child.wait(timeout=ceiling)
            outcome = "finished" if code == 0 else "failed"
            reason = "" if code == 0 else f"the tick script exited {code}"
            return code
        except subprocess.TimeoutExpired:
            outcome, code = "timeout", TIMED_OUT
            reason = f"the tick reached its {ceiling:.0f} s ceiling and was stopped"
            print(f"tick stopped: {reason}", file=sys.stderr)
            return TIMED_OUT
        except InterruptedError:
            outcome, code = "interrupted", 128 + interrupted[-1]
            reason = f"signal {interrupted[-1]} reached the supervisor"
            return code
        finally:
            # Keep the lock until the complete child process group has stopped.
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if child.poll() is None:
                _stop_group(child.pid, SUPERVISOR_GRACE)
                child.wait()
            # A step runner killed before it could stop its own group left the
            # group's id behind: that group goes too.
            try:
                pgid = int(_step_marker().read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pgid = 0
            if pgid > 1:
                _stop_group(pgid, 0.5)
                _step_marker().unlink(missing_ok=True)
            record_run(started, outcome, code, reason)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["acquire", "release", "status", "run", "allowed", "step"])
    ap.add_argument("arguments", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if args.action == "run":
        command = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        return supervised(command)
    if args.action == "step":
        # step [--ungated] NAME -- COMMAND...: the feature gate, then the watchdog.
        rest = list(args.arguments)
        gated = True
        if rest[:1] == ["--ungated"]:
            gated, rest = False, rest[1:]
        if len(rest) < 3 or rest[1] != "--":
            ap.error("step needs NAME -- COMMAND...")
        name, command = rest[0], rest[2:]
        if gated and not step_allowed(name):
            print("disabled in workspace settings", flush=True)
            return 0
        return bounded(name, command)
    if args.action == "allowed":
        return 0 if len(args.arguments) == 1 and step_allowed(args.arguments[0]) else 1
    return {"acquire": acquire, "release": release, "status": status}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
