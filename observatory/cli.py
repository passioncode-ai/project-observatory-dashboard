"""Agent-friendly commands with JSON results and safe default scopes."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .core import (ObservatoryError, add_project, config, discover, init, latest, osprivacy,
                   known_slot_values, private_file, public_export, redact, scan, state_path, write_json)
from .credentials import put_secret, run_secret, scan_leaks, secret_names


_OUTPUT_STATE = None


def emit(value):
    if _OUTPUT_STATE is not None:
        try:
            value = redact(value, known_slot_values(_OUTPUT_STATE))
        except (ObservatoryError, OSError, ValueError):
            value = {"error": "Private slots are unreadable; output was withheld to preserve the redaction boundary."}
    print(json.dumps(value, indent=2, ensure_ascii=False))


def doctor(state: Path) -> dict:
    checks = [{"name": "python", "ok": sys.version_info >= (3, 11)},
              {"name": "state_initialized", "ok": (state / "config.json").is_file()}]
    try:
        git = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        git_ok = git.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        git_ok = False
    checks.append({"name": "git_available", "ok": git_ok, "required": False})
    if (state / "config.json").is_file():
        try:
            cfg = config(state)
            private_file(state / "fingerprint-salt")
            checks.append({"name": "private_state", "ok": True})
            checks.append({"name": "registered_roots_available", "ok": all(Path(p["path"]).is_dir() for p in cfg["projects"])})
        except ObservatoryError:
            checks.append({"name": "private_state", "ok": False})
    return {"version": __version__, "checks": checks, "network_requests": 0,
            "note": "No provider credentials are required for local observation. No provider calls are made; locally known values may be read transiently to redact output."}


def demo(state: Path) -> dict:
    init(state)
    if config(state)["projects"]:
        raise ObservatoryError("Use an empty --home for the demo; existing projects are never replaced.")
    directory = state.with_name(state.name + "-demo-projects")
    if directory.exists():
        raise ObservatoryError("Demo sample directory already exists; choose a new --home.")
    project = directory / "sample-app"
    project.mkdir(parents=True, mode=0o700)
    (project / "README.md").write_text("# Synthetic demo project\n")
    (project / "package.json").write_text('{"name":"synthetic-example","dependencies":{"example-library":"1.0.0"}}\n')
    (project / ".gitignore").write_text(".env\n")
    # Plainly synthetic, no provider prefix and no relationship to a live key.
    synthetic = "SYNTHETIC_DEMO_VALUE_NOT_A_CREDENTIAL"
    (project / ".env").write_text(f"DEMO_TOKEN={synthetic}\n")
    osprivacy().make_private(project / ".env")
    transcript = directory / "synthetic-transcript.txt"
    transcript.write_text("Synthetic example: a value echoed into a local artifact.\n" + synthetic + "\n" + synthetic + "\n")
    try:
        for args in (("init", "-q"), ("add", "README.md", "package.json", ".gitignore"),
                     ("-c", "user.name=Demo", "-c", "user.email=demo@example.invalid", "commit", "-q", "-m", "Synthetic demo")):
            result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(project), *args],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                                    env={**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                                         "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                         "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
            if result.returncode:
                break
    except (OSError, subprocess.TimeoutExpired):
        pass
    add_project(state, "sample-app", str(project))
    put_secret(state, "DEMO_TOKEN", synthetic)
    report = scan(state)
    leaks = scan_leaks(state, [str(transcript)])
    from .dashboard import build
    page = build(state)
    return {"demo": "synthetic", "projects": len(report["projects"]),
            "known_value_occurrences": sum(t["occurrences"] for t in leaks["targets"]),
            "dashboard": str(page), "network_requests": 0,
            "sample_directory": str(directory), "next": "Run serve with the same --home to open the local overview."}


#: The names the package installs; `observatory` is the short one (0.6.3).
PROGRAMS = ("observatory", "project-observatory")


def program() -> str:
    name = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    return name if name in PROGRAMS else "project-observatory"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=program(), description="Local observation for agent-operated projects. "
                                "No implicit scans or provider requests. With no arguments, opens the dashboard.")
    p.add_argument("--version", action="version", version=__version__)
    # Two defaults, because the two command sets keep separate workspaces: `full`
    # (and the bare command, which opens its dashboard) never reads the portable one.
    p.add_argument("--home", help="Private state directory. Portable commands: otherwise OBSERVATORY_HOME "
                   "or ~/.local/share/project-observatory . `full` and the bare command: otherwise "
                   "OBSERVATORY_FULL_HOME, then OBSERVATORY_HOME, or ~/.local/share/project-observatory-full")
    cmds = p.add_subparsers(dest="cmd")
    for name in ("init", "doctor", "demo", "scan", "status", "dashboard", "export"):
        cmds.add_parser(name)
    serve = cmds.add_parser("serve")
    serve.add_argument("--port", type=int, default=47311)
    project = cmds.add_parser("project").add_subparsers(dest="action", required=True)
    add = project.add_parser("add")
    add.add_argument("name")
    add.add_argument("path")
    find = project.add_parser("discover")
    find.add_argument("root")
    find.add_argument("--depth", type=int, default=2)
    project.add_parser("list")
    secret = cmds.add_parser("secret").add_subparsers(dest="action", required=True)
    put = secret.add_parser("put")
    put.add_argument("name")
    secret.add_parser("list")
    run = secret.add_parser("run")
    run.add_argument("name")
    run.add_argument("--env", required=True, dest="variable")
    run.add_argument("command", nargs=argparse.REMAINDER)
    leak = cmds.add_parser("leaks").add_subparsers(dest="action", required=True)
    ls = leak.add_parser("scan")
    ls.add_argument("--file", action="append", required=True, dest="targets")
    ls.add_argument("--sqlite", action="store_true", help="Treat each explicit file as read-only SQLite")
    full = cmds.add_parser("full", add_help=False, help="Complete engine; separate private workspace")
    full.add_argument("-h", "--help", dest="engine_help", action="store_true")
    full.add_argument("arguments", nargs=argparse.REMAINDER)
    cmds.add_parser("full-path", help="Print the installed complete engine's source directory")
    return p


def open_dashboard(explicit_home: str | None = None) -> int:
    """No arguments: open the complete engine's dashboard when a workspace exists,
    otherwise say where to start. Nothing is scanned or requested either way —
    `full open` builds the pages from what the workspace already holds."""
    from .full_cli import full_home, run
    try:
        home = full_home(explicit_home)
    except Exception as exc:  # noqa: BLE001 — an unusable home is reported, not raised
        print(f"{program()}: {exc}", file=sys.stderr)
        return 2
    if (home / "workspace.json").is_file():
        return run(["open"], explicit_home)
    name = program()
    # Exit 2 and stderr: the dashboard was asked for and nothing was opened, so a
    # script testing the exit code must not read this as success.
    print(f"No Project Observatory workspace at {home}.\n"
          f"  {name} full init        create one, then `{name} full onboard` for the next steps\n"
          f"  {name} --home \"$HOME/observatory-demo\" demo   see a synthetic estate first, in a folder of its own\n"
          f"  {name} --home PATH ...  or OBSERVATORY_HOME=PATH to use a workspace elsewhere\n"
          f"Once it exists, `{name}` on its own opens the dashboard.", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    global _OUTPUT_STATE
    args = parser().parse_args(argv)
    if args.cmd is None:
        return open_dashboard(args.home)
    if args.cmd == "full-path":
        from .full_cli import engine_path
        print(engine_path())
        return 0
    if args.cmd == "full":
        from .full_cli import run
        return run(["--help"] if args.engine_help else args.arguments, args.home)
    try:
        state = state_path(args.home)
    except ObservatoryError as exc:
        emit({"error": str(exc)})
        return 2
    _OUTPUT_STATE = state
    try:
        if args.cmd == "init":
            cfg = init(state)
            emit({"initialized": True, "registered_projects": len(cfg["projects"]), "scans_performed": 0})
        elif args.cmd == "doctor":
            result = doctor(state)
            emit(result)
            return 0 if all(c["ok"] for c in result["checks"] if c.get("required", True)) else 2
        elif args.cmd == "demo":
            emit(demo(state))
        elif args.cmd == "project":
            if args.action == "add":
                emit(add_project(state, args.name, args.path))
            elif args.action == "discover":
                emit(discover(Path(args.root), args.depth))
            else:
                emit({"projects": config(state)["projects"], "private_output": True})
        elif args.cmd == "scan":
            emit(scan(state))
        elif args.cmd == "status":
            emit(latest(state))
        elif args.cmd == "export":
            emit(public_export(latest(state)))
        elif args.cmd == "dashboard":
            from .dashboard import build
            emit({"dashboard": str(build(state)), "private": True})
        elif args.cmd == "serve":
            from .dashboard import serve
            if not 0 <= args.port <= 65535:
                raise ObservatoryError("Port must be between 0 and 65535.")
            serve(state, args.port)
        elif args.cmd == "secret":
            if args.action == "put":
                # getpass writes only a prompt to the terminal. Agents should use local stdin.
                value = getpass.getpass("Secret value (hidden, local terminal only): ") if sys.stdin.isatty() else sys.stdin.read(64 * 1024 + 1)
                emit(put_secret(state, args.name, value))
            elif args.action == "list":
                emit({"names": secret_names(state), "values_returned": False})
            else:
                command = args.command[1:] if args.command[:1] == ["--"] else args.command
                result = run_secret(state, args.name, args.variable, command)
                emit(result)
                return result["exit_code"] if 0 <= result["exit_code"] <= 255 else 1
        elif args.cmd == "leaks":
            report = scan_leaks(state, args.targets, args.sqlite)
            emit(report)
            return 2 if report["degraded"] else (1 if report["findings"] else 0)
        return 0
    except ObservatoryError as exc:
        emit({"error": str(exc)})
        return 2
    except (OSError, ValueError, json.JSONDecodeError):
        # OS errors may include sensitive file names. Do not include exception text.
        emit({"error": "Operation could not complete. Check scope, paths, permissions and input format; no file contents were printed."})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
