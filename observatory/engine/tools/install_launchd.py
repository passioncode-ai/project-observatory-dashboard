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


#: A path component that names one Python release: `lib/python3.14/site-packages` on POSIX, and a
#: per-user `Python314` folder on Windows. A venv rebuilt on another release no longer has it (OBS-393).
_VERSIONED_PART = re.compile(r"python3\.?\d+t?", re.IGNORECASE)

#: The module that starts an engine program by name (`observatory/job.py`), and its table:
#: name -> (path inside the engine, how it runs). A test keeps the two tables equal.
LAUNCHER = "observatory.job"
JOB_TOOLS = {"maintain": ("tools/maintain.py", "python"), "tick": ("tools/tick.sh", "bash"),
             "tick-py": ("tools/tick.py", "python"), "serverd": ("tools/serverd.py", "python"),
             "mcp": ("mcp/server.py", "python")}


def version_bound(path) -> bool:
    """True when PATH has a component naming one Python release (`python3.14`)."""
    return any(_VERSIONED_PART.fullmatch(part) for part in pathlib.PurePath(str(path)).parts)


def engine_root() -> pathlib.Path:
    """The engine's code folder, as the configuration module names it (tests move it)."""
    return pathlib.Path(configuration.SOURCE)


def job_argv(tool: str, *args: str, python: str | None = None) -> list[str]:
    """The argv a job definition names to run engine program TOOL with ARGS (OBS-393).

    An engine installed under `<venv>/lib/python3.X/site-packages` is started through the
    package's launcher, `<venv>/bin/python3 -P -m observatory.job TOOL ARGS`: the definition names
    the venv's stable interpreter and a program name, nothing a rebuild on another Python release
    removes. A checkout's paths name no release, so its jobs keep naming the file itself."""
    python = python or stable_interpreter(configuration.engine_python())
    root = engine_root()
    if version_bound(root):
        return [python, "-P", "-m", LAUNCHER, tool, *args]
    relative, kind = JOB_TOOLS[tool]
    return (["/bin/bash", str(root / relative)] if kind == "bash" else [python, str(root / relative)]) + list(args)


def job_cwd(workspace: pathlib.Path | str) -> pathlib.Path:
    """A job's working folder: the workspace home when the engine sits in a versioned
    site-packages folder (the launcher moves into the engine itself), else the engine as before."""
    root = engine_root()
    return pathlib.Path(workspace) if version_bound(root) else root


#: Definition keys holding one path each; ProgramArguments is read separately.
_PATH_KEYS = ("Program", "WorkingDirectory")


def missing_paths(doc: dict) -> list[str]:
    """The absolute paths a launchd definition names that do not exist (OBS-393): `Program`,
    `ProgramArguments[0]`, every absolute argument after it, and `WorkingDirectory`. A bare
    program name is launchd's PATH lookup, and an option (`-m`) is not a path."""
    if not isinstance(doc, dict):
        return []
    args = doc.get("ProgramArguments")
    named = [doc.get(key) for key in _PATH_KEYS]
    if isinstance(args, list):
        named += [a for a in args if isinstance(a, str)]
    missing: list[str] = []
    for value in named:
        if isinstance(value, str) and value and os.path.isabs(value) and value not in missing \
                and not os.path.exists(value):
            missing.append(value)
    return missing


def venv_home_problem(root: pathlib.Path | None = None) -> str | None:
    """The engine's virtual environment whose `pyvenv.cfg` `home` is gone, as a sentence; None when
    there is no venv or its base interpreter's folder exists. Every interpreter link of that venv
    points into the missing folder, so no job of it can start (OBS-393)."""
    root = root or engine_root()
    for parent in root.parents:
        cfg = parent / "pyvenv.cfg"
        if not cfg.is_file():
            continue
        try:
            lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        for line in lines:
            key, _, value = line.partition("=")
            if key.strip().lower() == "home" and value.strip():
                home = value.strip()
                if not os.path.isdir(home):
                    return (f"the engine's virtual environment {parent} was made from a Python that is gone "
                            f"(pyvenv.cfg home {home} does not exist)")
                return None
        return None
    return None


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


def environment(python: str | None = None) -> dict[str, str]:
    # OBSERVATORY_PYTHON: the tick runs every step with the interpreter the engine
    # is installed in, not the installer's and not whatever python3 is first on PATH.
    env = {"OBSERVATORY_PYTHON": python or stable_interpreter(configuration.engine_python()),
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


def build(interval: int, python: str | None = None) -> dict:
    if interval < 60:
        raise ValueError("Tick interval must be at least 60 seconds")
    python = python or stable_interpreter(configuration.engine_python())
    env = environment(python)
    env["OBSERVATORY_TICK_CEILING_SECONDS"] = str(tick_ceiling(interval))
    return {
        "Label": LABEL,
        # Version-free (OBS-393): the venv's interpreter and the launcher's program name, or
        # `/bin/bash tools/tick.sh` in a checkout, whose path names no Python release.
        "ProgramArguments": job_argv("tick", python=python),
        "StartInterval": interval,
        "RunAtLoad": False,          # a login is not a reason to burn a scan
        "WorkingDirectory": str(job_cwd(paths.HOME)),
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


#: The server's SIGTERM drain budget (tools/serverd.py EXIT_TIMEOUT); launchd's ExitTimeOut sits above it.
SERVER_EXIT_TIMEOUT = 30


def default_server_port() -> int:
    return int(os.environ.get("OBSERVATORY_SERVER_PORT", "47311"))


def server_document(port: int, python: str | None = None) -> dict:
    """The server's launchd definition (tools/serverd.py `build_plist`), here so the maintenance
    pass can rebuild it without importing the server."""
    python = python or stable_interpreter(configuration.engine_python())
    return {
        "Label": instance_label("server"),
        # The venv's interpreter or a keg's `opt` link, never a Cellar path (LC-05), and the
        # launcher's program name, never a path naming one Python release (OBS-393).
        "ProgramArguments": job_argv("serverd", "--run", "--port", str(port), python=python),
        "RunAtLoad": True,
        "KeepAlive": True,
        # fabric-service/0.1 supervisor rules: a crash loop is bounded, and
        # SIGKILL comes only after the SIGTERM drain has had its time.
        "ThrottleInterval": 10,
        "ExitTimeOut": SERVER_EXIT_TIMEOUT + 10,
        # STANDARD, NOT BACKGROUND. The server answers Fabric Dashboards' probe and
        # every agent's MCP call while the Mac is busy. As a Background job with
        # Nice 5 and low-priority I/O, macOS starved it under load: on 2026-10-01
        # the host recorded it "not answering" 30 times in a day while the process
        # ran without one restart. The tick stays background work; the server is not.
        "ProcessType": "Standard",
        "StandardErrorPath": str(paths.STATE / "logs/serverd.err"),
        "StandardOutPath": str(paths.STATE / "logs/serverd.out"),
        "EnvironmentVariables": environment(python),
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


def kick_job(label: str) -> tuple[bool, str]:
    """Run a loaded job now (`kickstart` without `-k`: a running instance is left alone)."""
    code, out = _launchctl("kickstart", f"gui/{uid()}/{label}")
    return code == 0, out


def job_running(label: str) -> bool:
    """launchd reports the job's process as running right now."""
    code, out = _launchctl("print", f"gui/{uid()}/{label}")
    return code == 0 and "state = running" in out


def _is_python(argument: str) -> bool:
    return pathlib.PurePath(str(argument)).name.startswith("python")


#: Environment entries that come from whoever runs the builder (a terminal, an agent session, the
#: maintenance pass): a repair keeps the installed job's own, so a definition never flips with
#: its caller (maintenance.py, review F1).
CALLER_ENV = ("PATH", *TICK_LIMITS)


def _port_of(doc: dict) -> int | None:
    args = doc.get("ProgramArguments") or []
    for i, arg in enumerate(args[:-1]):
        if arg == "--port" and str(args[i + 1]).isdigit():
            return int(args[i + 1])
    return None


def wanted_definition(name: str, installed: dict, python: str | None = None) -> dict | None:
    """The definition the installer would write now for managed job NAME, fitted to what is
    installed: the tick keeps its interval, the server its port, both their caller's environment
    entries (CALLER_ENV). None when it cannot be built here (a refused interpreter path)."""
    try:
        if name == "tick":
            interval = installed.get("StartInterval")
            doc = build(interval if isinstance(interval, int) and interval >= 60 else 1800, python=python)
        elif name == "server":
            doc = server_document(_port_of(installed) or default_server_port(), python=python)
        else:
            return None
    except (configuration.ConfigurationError, ValueError):
        return None
    env, have = doc["EnvironmentVariables"], installed.get("EnvironmentVariables") or {}
    for key in CALLER_ENV:
        if isinstance(have.get(key), str):
            env[key] = have[key]
        elif key != "PATH":
            env.pop(key, None)
    return doc


def _drift(installed: dict, wanted: dict) -> tuple[str, str]:
    """(was, now) for a report: the interpreter when that changed, else the program line."""
    def python_of(doc):
        env = doc.get("EnvironmentVariables") or {}
        args = doc.get("ProgramArguments") or []
        return env.get("OBSERVATORY_PYTHON") or (args[0] if args and _is_python(args[0]) else None)
    old, new = python_of(installed), python_of(wanted)
    if old and new and old != new:
        return old, new
    return (" ".join(map(str, installed.get("ProgramArguments") or [])),
            " ".join(map(str, wanted.get("ProgramArguments") or [])))


def repair_definitions(expected: str | None = None, *, write: bool = True,
                       only: set[str] | None = None) -> list[dict]:
    """The managed jobs (tick, server) whose installed plist differs from the one the installer
    would write now, each rewritten in place with `write` (mode 0600; loading it is the caller's step).

    The whole definition is compared, not one field. Before 0.19.3 the plists could name the
    INSTALLER's interpreter (a tick on Homebrew's bare python failed its backup on `No module
    named 'cryptography'`), and only the interpreter was repaired; a venv rebuilt on another
    Python release (3.14 to 3.15) left every plist naming `lib/python3.14/site-packages/...`
    files that no longer existed, and launchd failed each run with exit 78 (OBS-393). `full
    update` restarts the jobs from the files as they are, so only this rewrite mends them.
    `expected` is the interpreter the definitions name (the engine's by default); `only` names
    the jobs to look at. A job without a plist, or with one that does not parse, is skipped."""
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
        if not isinstance(doc, dict):
            continue
        wanted = wanted_definition(job["name"], doc, python=expected)
        if wanted is None or wanted == doc or lint_plist(wanted):
            continue
        was, now = _drift(doc, wanted)
        found.append({"name": job["name"], "label": job["label"], "plist": str(path), "was": was, "now": now,
                      "changed": sorted(k for k in set(doc) | set(wanted) if doc.get(k) != wanted.get(k)),
                      **({"missing": missing_paths(doc)} if missing_paths(doc) else {})})
        if write:
            fd = osprivacy.open(path, os.O_WRONLY | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(plistlib.dumps(wanted))
            os.chmod(path, 0o600)
    return found


#: The name the maintenance pass and older callers use; the comparison is the whole definition now.
repair_interpreters = repair_definitions


def tick_job(interval: int):
    """The tick for Task Scheduler or systemd (`osschedule`), as `build` describes it for launchd."""
    import osschedule
    if interval < 60:
        raise ValueError("Tick interval must be at least 60 seconds")
    env = environment()
    env["OBSERVATORY_TICK_CEILING_SECONDS"] = str(tick_ceiling(interval))
    return osschedule.Job(name="tick", workspace=paths.HOME, argv=tuple(job_argv("tick-py")),
                          env=tuple(sorted(env.items())), cwd=job_cwd(paths.HOME),
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
