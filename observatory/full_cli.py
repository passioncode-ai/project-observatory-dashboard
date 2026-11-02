"""Launch the complete engine without importing its flat modules into the SDK."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys


def engine_path() -> Path:
    return Path(__file__).resolve().parent / "engine"


def full_home(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("OBSERVATORY_FULL_HOME") or os.environ.get("OBSERVATORY_HOME")
    from .core import ObservatoryError, osprivacy, refuse_home_inside_code
    path = Path(value).expanduser() if value else osprivacy().default_home("project-observatory-full")
    if not path.is_absolute():
        raise ValueError("Full workspace path must be absolute")
    try:
        refuse_home_inside_code(path)
    except ObservatoryError as exc:
        raise ValueError(str(exc)) from None
    return path


def engine_environment(home: Path, root: Path, *, nt: bool | None = None) -> dict[str, str]:
    """The environment the complete engine runs with: this one, its home and its code named.

    On Windows also UTF-8 mode: the engine reads and writes UTF-8 throughout, and Windows' default
    is the ANSI code page (cp1252), under which a page or log holding a non-Latin-1 character does not
    read back. The scheduled tasks already run this way (`osschedule`); a person's command now does
    too (OBS-277). A value the person set is kept."""
    env = dict(os.environ, OBSERVATORY_HOME=str(home), OBSERVATORY_ROOT=str(root))
    if os.name == "nt" if nt is None else nt:
        env.setdefault("PYTHONUTF8", "1")
    return env


def run(argv: list[str], explicit_home: str | None = None) -> int:
    root = engine_path()
    if not (root / "observatory.py").is_file():
        print("Complete engine is missing from this installation.", file=sys.stderr)
        return 2
    try:
        home = full_home(explicit_home)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # Old portable state is never implicitly treated as a full workspace.
    if (home / "config.json").exists() and not (home / "workspace.json").exists():
        print("This is a portable 0.1 workspace. Keep it for the existing commands and select a separate --home for full.", file=sys.stderr)
        return 2
    env = engine_environment(home, root)
    try:
        result = subprocess.run([sys.executable, str(root / "observatory.py"), *(argv or ["--help"])], env=env)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except OSError:
        print("Could not launch the complete engine; check the Python installation.", file=sys.stderr)
        return 2
