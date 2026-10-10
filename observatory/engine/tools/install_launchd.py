#!/usr/bin/env python3
"""Install, remove or inspect the launchd job that ticks the observatory.

Every path is absolute and computed from this file, so the plist is correct
wherever the checkout lives. `--interval` is seconds between ticks; the default
is 30 minutes because a tick over a quiet machine costs nothing and the
interesting resolution here is "did work happen this half hour", not seconds.
"""
from __future__ import annotations
import argparse, plistlib, subprocess, sys, os, pathlib, hashlib, re, shutil, stat

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import osprivacy  # noqa: E402
import paths
import configuration


def instance_label(kind: str) -> str:
    digest = hashlib.sha256(str(paths.HOME.resolve()).encode()).hexdigest()[:16]
    return f"org.project-observatory.{digest}.{kind}"


def plist_path(label: str) -> pathlib.Path:
    return pathlib.Path.home() / "Library/LaunchAgents" / f"{label}.plist"


LABEL = instance_label("tick")
PLIST = plist_path(LABEL)
LOG_DIR = paths.STATE / "logs"


def scheduler_allowed() -> bool:
    return configuration.enabled("scheduler", "features")


SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


#: The programs the engine's jobs run by name. Only the directories that hold one
#: of these join the plist's PATH (lifecycle LC-05). `claude` is listed for the
#: on-demand MCP probe a person may start through the server, never the tick.
TOOLS = ("git", "gh", "heroku", "wrangler", "node", "claude", "dig", "curl", "witr", "lsof", "xcrun")


def _safe_dir(entry: str) -> bool:
    """An absolute, existing directory no other account can write."""
    if not entry or not os.path.isabs(entry):
        return False
    try:
        info = os.stat(entry)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
        return False
    return not (info.st_mode & 0o020 and info.st_uid not in (os.getuid(), 0))


def launch_path(current: str | None = None) -> str:
    """A minimal PATH for launchd jobs: the directories holding the engine's TOOLS, then the system ones.

    launchd starts jobs with a bare PATH, so tools the collectors call - `heroku`,
    `gh`, `wrangler` - vanished whenever they lived in ~/.local/bin or a version
    manager's directory. The installer's whole PATH used to be copied instead, and
    with it every directory an interactive shell had collected: plugin `bin`
    folders of versions since deleted were measured in both plists on 2026-10-03.
    Now only a directory of the installer's PATH that holds one of TOOLS is kept, in
    the installer's order, followed by the system directories. Every entry must be
    absolute, existing and writable by no other account: never world-writable, and
    group-writable only when owned by this user or root (Homebrew's
    /opt/homebrew/bin is user-owned, admin-writable). A PATH entry another account
    can write lets it plant a binary the job runs. See docs/ONBOARDING.md "Enable
    background ...".
    """
    source = current if current is not None else os.environ.get("PATH", "")
    out: list[str] = []
    for entry in source.split(os.pathsep):
        if entry in out or entry in SYSTEM_PATH or not _safe_dir(entry):
            continue
        if any(os.access(os.path.join(entry, tool), os.X_OK) for tool in TOOLS):
            out.append(entry)
    out += [entry for entry in SYSTEM_PATH if entry not in out and _safe_dir(entry)]
    return os.pathsep.join(out)


#: `<prefix>/Cellar/<formula>/<version>/<rest>`: a Homebrew keg by its versioned path.
_CELLAR = re.compile(r"^(?P<prefix>.*)/Cellar/(?P<formula>[^/]+)/[^/]+/(?P<rest>.+)$")


def stable_interpreter(executable: str) -> str:
    """The interpreter path a plist may name: never a Homebrew Cellar path.

    A Cellar path names one version (`…/Cellar/python@3.14/3.14.7/…`) and is
    deleted by the next `brew upgrade`, leaving a job that cannot start. The keg's
    `opt` link (`…/opt/python@3.14/…`) survives upgrades; a virtual environment's
    `bin/python` is used as it is. The path is never resolved through symlinks:
    resolving is exactly what turns a stable link back into a Cellar path.

    What this does NOT fix (lifecycle LC-05, deferred): macOS privacy consents are
    keyed to the code identity of the binary that runs, and Homebrew's python is
    ad hoc signed, so an upgrade still voids them — a Developer ID signed launcher
    is the fix, recorded in AGENTS.md → Lifecycle."""
    m = _CELLAR.match(executable)
    if not m:
        return executable
    linked = f"{m.group('prefix')}/opt/{m.group('formula')}/{m.group('rest')}"
    if os.path.exists(linked):
        return linked
    raise configuration.ConfigurationError(
        f"The engine runs on a versioned Homebrew path ({executable}) with no `opt` link to use "
        "instead; install it into a virtual environment and run the installer from there")


def lint_plist(doc: dict) -> list[str]:
    """What makes a plist unfit to install: a Cellar path in its program or environment (LC-05)."""
    problems = []
    for i, arg in enumerate(doc.get("ProgramArguments") or []):
        if "/Cellar/" in str(arg):
            problems.append(f"ProgramArguments[{i}] names a versioned Homebrew path: {arg}")
    for key, value in (doc.get("EnvironmentVariables") or {}).items():
        if "/Cellar/" in str(value):
            problems.append(f"EnvironmentVariables.{key} names a versioned Homebrew path")
    return problems


#: Numeric limits a tick step reads from its environment. launchd starts the job
#: with the plist's environment only, so a limit raised in the shell that installs
#: the job must travel in the plist. Seconds, 1 to 3600, and nothing else: see
#: `build()` for why no key ever goes here. collectors/scan_mcp.py reads the first.
TICK_LIMITS = ("OBSERVATORY_MCP_PROBE_TIMEOUT",)


def _seconds(value: str) -> bool:
    try:
        return 1 <= float(value) <= 3600
    except ValueError:
        return False


def environment() -> dict[str, str]:
    # OBSERVATORY_PYTHON: the tick runs every step with the interpreter the engine
    # is installed in, not the installer's and not whatever python3 is first on PATH.
    env = {"OBSERVATORY_PYTHON": stable_interpreter(configuration.engine_python()),
           "OBSERVATORY_HOME": str(paths.HOME)}
    if os.name != "nt":
        # A Windows task runs with the account's own environment, PATH and profile included;
        # launchd and systemd start a job with a bare one.
        env = {"PATH": launch_path(), **env, "HOME": str(pathlib.Path.home())}
    for name in TICK_LIMITS:
        value = os.environ.get(name, "").strip()
        if value and _seconds(value):
            env[name] = value
    return env


def prepare_logs(names: tuple[str, ...], directory: pathlib.Path = LOG_DIR) -> None:
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise configuration.ConfigurationError("Log directory cannot be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    for name in names:
        fd = osprivacy.open(directory / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)


def uid() -> int:
    return os.getuid()


def tick_ceiling(interval: int) -> int:
    """The tick's whole-run ceiling: the supervisor's default, and always under the interval."""
    sys.path.insert(0, str(ROOT / "tools"))
    import tick_lease
    return int(min(tick_lease.DEFAULT_CEILING_SECONDS, interval * 5 // 6))


def exit_timeout() -> int:
    """launchd's SIGTERM-to-SIGKILL wait for the tick, above everything the supervisor waits.

    launchd printed `exit timeout = 5` for a plist without the key while the
    supervisor waited 10 s for its children: on a bootout launchd SIGKILLed the
    supervisor first and the children ran on with the lock released (F6)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import tick_lease
    return int(tick_lease.SUPERVISOR_GRACE + tick_lease.STEP_GRACE + 15)


def build(interval: int) -> dict:
    if interval < 60:
        raise ValueError("Tick interval must be at least 60 seconds")
    env = environment()
    env["OBSERVATORY_TICK_CEILING_SECONDS"] = str(tick_ceiling(interval))
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(ROOT / "tools/tick.sh")],
        "StartInterval": interval,
        "RunAtLoad": False,          # a login is not a reason to burn a scan
        "WorkingDirectory": str(ROOT),
        "StandardOutPath": str(LOG_DIR / "tick.log"),
        "StandardErrorPath": str(LOG_DIR / "tick.err"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "ExitTimeOut": exit_timeout(),
        # PATH and HOME only, and no secret ever. A plist is a world-readable
        # file: a key in EnvironmentVariables here would be plaintext where every
        # process on the machine can read it. The agent resolves its key at run
        # time from the machine's secret store — see agent/providers.py
        # KEY_FILES, and tests/test_key.py which asserts this dict stays clean.
        "EnvironmentVariables": env,
        "Umask": 0o077,
    }


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def managed_jobs() -> list[dict]:
    """This workspace's two launchd jobs: the tick (here) and the server (tools/serverd.py).

    `full update` stops and restarts exactly these. Their labels come from the
    workspace path, so another workspace's jobs on the same account are never named."""
    server = instance_label("server")
    return [{"name": "tick", "label": LABEL, "plist": str(PLIST)},
            {"name": "server", "label": server, "plist": str(plist_path(server))}]


def _launchctl(*args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 125, type(exc).__name__
    return p.returncode, (p.stdout + p.stderr).strip()


def job_loaded(label: str) -> bool:
    return _launchctl("print", f"gui/{uid()}/{label}")[0] == 0


def stop_job(label: str) -> tuple[bool, str]:
    """Boot the job out, leaving its plist on disk so start_job can bring it back."""
    code, out = _launchctl("bootout", f"gui/{uid()}/{label}")
    return code == 0 or not job_loaded(label), out


def start_job(plist: str) -> tuple[bool, str]:
    code, out = _launchctl("bootstrap", f"gui/{uid()}", plist)
    return code == 0, out


def restart_job(label: str) -> tuple[bool, str]:
    """Stop the loaded job's process and start it again from its plist (`kickstart -k`)."""
    code, out = _launchctl("kickstart", "-k", f"gui/{uid()}/{label}")
    return code == 0, out


def job_running(label: str) -> bool:
    """launchd reports the job's process as running right now."""
    code, out = _launchctl("print", f"gui/{uid()}/{label}")
    return code == 0 and "state = running" in out


def _is_python(argument: str) -> bool:
    return pathlib.PurePath(str(argument)).name.startswith("python")


def repair_interpreters(expected: str | None = None, *, write: bool = True,
                        only: set[str] | None = None) -> list[dict]:
    """The managed jobs whose plist names an interpreter other than the engine's.

    The tick's plist carries it in `OBSERVATORY_PYTHON`, the server's as its program.
    Before 0.19.3 both were written with the INSTALLER's interpreter, which is not the
    engine's when anything other than the engine's venv started the installer; a tick
    on Homebrew's bare python then failed its backup on `No module named
    'cryptography'` and nothing ever rewrote the plist — `full update` restarts the
    jobs from the files as they are. With `write`, each such field is set to
    `expected` (the engine's interpreter by default) and the file rewritten in place,
    mode 0600; loading it is the caller's step. `only` names the jobs to look at.
    A job without a plist is skipped."""
    expected = expected or stable_interpreter(configuration.engine_python())
    found = []
    for job in managed_jobs():
        path = pathlib.Path(job["plist"])
        if (only is not None and job["name"] not in only) or path.is_symlink() or not path.is_file():
            continue
        try:
            doc = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        before = []
        env = doc.get("EnvironmentVariables") or {}
        if env.get("OBSERVATORY_PYTHON") not in (None, expected):
            before.append(env["OBSERVATORY_PYTHON"])
            env["OBSERVATORY_PYTHON"] = expected
        args = doc.get("ProgramArguments") or []
        if args and _is_python(args[0]) and args[0] != expected:
            before.append(args[0])
            args[0] = expected
        if not before:
            continue
        found.append({"name": job["name"], "label": job["label"], "plist": str(path),
                      "was": before[0], "now": expected})
        if write:
            fd = osprivacy.open(path, os.O_WRONLY | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(plistlib.dumps(doc))
            os.chmod(path, 0o600)
    return found


def tick_job(interval: int):
    """The tick for Task Scheduler or systemd (`osschedule`), as `build` describes it for launchd."""
    import osschedule
    if interval < 60:
        raise ValueError("Tick interval must be at least 60 seconds")
    env = environment()
    env["OBSERVATORY_TICK_CEILING_SECONDS"] = str(tick_ceiling(interval))
    return osschedule.Job(name="tick", workspace=paths.HOME,
                          argv=(stable_interpreter(configuration.engine_python()), str(ROOT / "tools/tick.py")),
                          env=tuple(sorted(env.items())), cwd=ROOT,
                          stdout=LOG_DIR / "tick.log", stderr=LOG_DIR / "tick.err",
                          interval_seconds=interval, time_limit_seconds=tick_ceiling(interval) + exit_timeout())


def main_other(action: str, interval: int) -> int:
    """install | uninstall | status | run-now on Windows (Task Scheduler) and Linux (systemd)."""
    import osschedule
    try:
        job = osschedule.supervisor(tick_job(interval))
        if action == "status":
            print(job.status())
            log = LOG_DIR / "tick.log"
            if log.exists():
                print(f"\nlast lines of {log}:")
                print("\n".join(log.read_text(errors="replace").splitlines()[-12:]))
            return 0
        if action == "uninstall":
            print(job.uninstall())
            return 0
        if action == "run-now":
            ok, out = job.start()
            print(out or ("started" if ok else "not started"))
            return 0 if ok else 1
        prepare_logs(("tick.log", "tick.err"))
        result = job.install()
    except (osschedule.ScheduleError, configuration.ConfigurationError, ValueError) as exc:
        print(f"Not installed: {exc}", file=sys.stderr)
        return 1
    print(f"installed: the tick every {interval // 60} min ({result})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["install", "uninstall", "status", "run-now"])
    ap.add_argument("--interval", type=int, default=1800)
    args = ap.parse_args()
    if args.action in {"install", "run-now"} and not scheduler_allowed():
        print("Scheduler disabled: enable features.scheduler in the workspace settings first", file=sys.stderr)
        return 1
    if sys.platform != "darwin":
        return main_other(args.action, args.interval)
    target = f"gui/{uid()}/{LABEL}"

    if args.action == "status":
        code, out = run("launchctl", "print", target)
        if code != 0:
            print(f"not loaded ({LABEL})")
            print(f"plist on disk: {PLIST.exists()}")
            return 0
        for line in out.splitlines():
            if any(k in line for k in ("state =", "runs =", "last exit", "path =")):
                print(" ", line.strip())
        log = LOG_DIR / "tick.log"
        if log.exists():
            print(f"\nlast lines of {log}:")
            print("\n".join(log.read_text(errors="replace").splitlines()[-12:]))
        return 0

    if args.action == "run-now":
        code, out = run("launchctl", "kickstart", target)
        print(out or f"kickstarted {LABEL} (exit {code})")
        return 0

    if args.action == "uninstall":
        run("launchctl", "bootout", target)
        PLIST.unlink(missing_ok=True)
        print(f"removed {LABEL} and its plist")
        return 0

    try:
        doc = build(args.interval)
    except configuration.ConfigurationError as exc:
        print(f"Not installed: {exc}", file=sys.stderr)
        return 1
    problems = lint_plist(doc)
    if problems:
        print("Not installed: " + "; ".join(problems), file=sys.stderr)
        return 1
    payload = plistlib.dumps(doc)
    prepare_logs(("tick.log", "tick.err"))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.is_symlink():
        raise configuration.ConfigurationError("LaunchAgent plist cannot be a symbolic link")
    fd = osprivacy.open(PLIST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    run("launchctl", "bootout", target)                                        
    code, out = run("launchctl", "bootstrap", f"gui/{uid()}", str(PLIST))
    if code != 0:
        print(f"bootstrap failed: {out}", file=sys.stderr)
        return 1
    print(f"installed {LABEL}, every {args.interval}s")
    print(f"  plist  {PLIST}")
    print(f"  log    {LOG_DIR / 'tick.log'}")
    print("  RunAtLoad is false on purpose: a login is not a reason to burn a scan.")
    # The maintenance job (updates, the app, a daily encrypted backup) comes with the
    # tick: a person who installs background jobs gets the one that keeps them current.
    import maintenance
    try:
        done = maintenance.ensure(paths.HOME)
        sched = done.get("schedule") or {}
        print(f"  maintenance: {sched.get('label') or sched.get('result')} "
              f"(automatic updates {'on' if done.get('auto_update') else 'off'})")
    except Exception as exc:  # noqa: BLE001 — the tick is installed either way
        print(f"  maintenance not scheduled: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
