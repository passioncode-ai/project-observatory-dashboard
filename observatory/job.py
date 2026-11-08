"""Start one engine program from a scheduled job's definition, by name (OBS-393).

`python -P -m observatory.job maintain run` runs `<engine>/tools/maintain.py run`, where
`<engine>` is the engine this interpreter has installed NOW. A job definition then names only
the virtual environment's own interpreter and a program name, never a path inside
`<venv>/lib/python3.X/site-packages`: those paths name one Python release, and a venv rebuilt
on another one (3.14 to 3.15) left every launchd job pointing at files that no longer existed
(launchd exit 78 on every run).

The program runs as it did when the definition named its file: on POSIX this process is
replaced by `python <file> ARGS` (or `/bin/bash tools/tick.sh` for the tick), with the same
arguments, environment and process id, so the scheduler's signals, its process group and every
process listing that matches the script path see what they saw before. Windows has no exec
that keeps the process, so the program runs in this process there (`runpy`).

`-P` keeps the job's working directory (the workspace home) off `sys.path`, so nothing in that
folder can stand in for this package. Standard library only; importing it does no I/O.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys

#: name -> (path inside the engine, how it runs, whether it starts in the engine folder).
#: The engine's `tools/install_launchd.py` keeps the same table (JOB_TOOLS); a test compares them.
TOOLS = {
    "maintain": ("tools/maintain.py", "python", True),
    "tick": ("tools/tick.sh", "bash", True),
    "tick-py": ("tools/tick.py", "python", True),
    "serverd": ("tools/serverd.py", "python", True),
    "mcp": ("mcp/server.py", "python", False),
}

USAGE = "usage: python -P -m observatory.job {%s} [ARGUMENTS...]" % ",".join(TOOLS)


def engine_root() -> Path:
    """The complete engine this interpreter has installed, resolved as `full-path` prints it."""
    return Path(__file__).resolve().parent / "engine"


def command(name: str, args: list[str], root: Path | None = None) -> tuple[list[str], Path | None]:
    """(argv, folder to start in) for program NAME; ValueError for a name this table lacks."""
    if name not in TOOLS:
        raise ValueError(f"unknown program {name!r}")
    relative, kind, chdir = TOOLS[name]
    root = root or engine_root()
    target = root / relative
    if kind == "bash" and os.name == "nt":
        # The tick's wrapper is bash; Windows runs the Python tick it hands over to.
        target, kind = root / "tools" / "tick.py", "python"
    argv = (["/bin/bash", str(target)] if kind == "bash" else [sys.executable, str(target)]) + list(args)
    return argv, (root if chdir else None)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr if not argv else sys.stdout)
        return 0 if argv else 2
    try:
        program, start_in = command(argv[0], argv[1:])
    except ValueError as exc:
        print(f"observatory.job: {exc}; {USAGE}", file=sys.stderr)
        return 2
    target = Path(program[1])
    if not target.is_file():
        print(f"observatory.job: {target} is missing from this installation; reinstall the package", file=sys.stderr)
        return 2
    if start_in is not None:
        os.chdir(start_in)
    if os.name != "nt":
        os.execv(program[0], program)
    # Windows: the script runs here, as `python <file>` would run it.
    sys.argv = program[1:]
    sys.path.insert(0, str(target.parent))
    try:
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
