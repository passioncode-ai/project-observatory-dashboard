#!/usr/bin/env python3
"""Install, remove or inspect the launchd job that ticks the observatory.

Every path is absolute and computed from this file, so the plist is correct
wherever the checkout lives. `--interval` is seconds between ticks; the default
is 30 minutes because a tick over a quiet machine costs nothing and the
interesting resolution here is "did work happen this half hour", not seconds.
"""
from __future__ import annotations
import argparse, plistlib, subprocess, sys, os, pathlib, hashlib, stat

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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


def launch_path(current: str | None = None) -> str:
    """PATH for launchd jobs: the installing user's directories, then the system ones.

    launchd starts jobs with a bare PATH, so tools the collectors call - `claude`
    (MCP inventory), `heroku`, `gh`, `wrangler` - vanished whenever they lived in
    ~/.local/bin or a version manager's directory. The installer's own PATH is
    kept in its order, restricted to absolute, existing directories that no other
    account can write: never world-writable, and group-writable only when owned by
    this user or root (Homebrew's /opt/homebrew/bin is user-owned, admin-writable).
    A PATH entry another account can write lets it plant a binary the job runs.
    See docs/ONBOARDING.md "Enable background ...".
    """
    out: list[str] = []
    for entry in [*(current if current is not None else os.environ.get("PATH", "")).split(os.pathsep),
                  *SYSTEM_PATH]:
        if not entry or not os.path.isabs(entry) or entry in out:
            continue
        try:
            info = os.stat(entry)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o002:
            continue
        if info.st_mode & 0o020 and info.st_uid not in (os.getuid(), 0):
            continue
        out.append(entry)
    return os.pathsep.join(out)


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
    # OBSERVATORY_PYTHON: tick.sh runs every step with the interpreter that
    # installed this engine, not whatever python3 is first on PATH.
    env = {"PATH": launch_path(), "OBSERVATORY_PYTHON": sys.executable,
           "HOME": str(pathlib.Path.home()), "OBSERVATORY_HOME": str(paths.HOME)}
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
        fd = os.open(directory / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)


def uid() -> int:
    return os.getuid()


def build(interval: int) -> dict:
    if interval < 60:
        raise ValueError("Tick interval must be at least 60 seconds")
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
        # PATH and HOME only, and no secret ever. A plist is a world-readable
        # file: a key in EnvironmentVariables here would be plaintext where every
        # process on the machine can read it. The agent resolves its key at run
        # time from the machine's secret store — see agent/providers.py
        # KEY_FILES, and tests/test_key.py which asserts this dict stays clean.
        "EnvironmentVariables": environment(),
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["install", "uninstall", "status", "run-now"])
    ap.add_argument("--interval", type=int, default=1800)
    args = ap.parse_args()
    target = f"gui/{uid()}/{LABEL}"
    if args.action in {"install", "run-now"} and not scheduler_allowed():
        print("Scheduler disabled: enable features.scheduler in the workspace settings first", file=sys.stderr)
        return 1
    if sys.platform != "darwin":
        print("launchd is supported on macOS only", file=sys.stderr)
        return 1

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

    payload = plistlib.dumps(build(args.interval))
    prepare_logs(("tick.log", "tick.err"))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.is_symlink():
        raise configuration.ConfigurationError("LaunchAgent plist cannot be a symbolic link")
    fd = os.open(PLIST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
