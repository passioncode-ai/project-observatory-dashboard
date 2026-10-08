#!/usr/bin/env python3
"""Keep `requirements-full.lock` in step with the `[full]` pins in `pyproject.toml` (OBS-17).

Dependabot's pip ecosystem rewrites a pin in `pyproject.toml` and does not know the lock, so
every such pull request failed at `pip install -c requirements-full.lock '.[full]'` on all
three CI rows (#24, #83, #179) with a resolver error that named neither file. The lock keeps
its name: installed engines read `observatory/engine/requirements-full.lock` from a new wheel
when they update, and a renamed lock would make them install it unconstrained.

    python tools/refresh_lock.py --check   # fast, offline: every exact pin is in the lock
    python tools/refresh_lock.py           # rebuild the lock in a fresh virtual environment

The rebuild installs `.[full]` into a temporary venv with the interpreter running this
script, and writes `pip freeze` (without this package) under the lock's header. It needs the
network; CI runs only `--check`.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib; the project requires 3.11
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-full.lock"
HEADER = "# Tested full-engine dependency constraints; refresh deliberately with the compatibility matrix."
PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(requirements: list[str]) -> dict[str, str]:
    out = {}
    for req in requirements:
        m = PIN.match(req)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


def pinned(pyproject_text: str) -> dict[str, str]:
    """The exact (`==`) pins of the project's dependencies and its `full` extra."""
    doc = tomllib.loads(pyproject_text)
    project = doc.get("project") or {}
    reqs = list(project.get("dependencies") or []) + list((project.get("optional-dependencies") or {}).get("full") or [])
    return _pins(reqs)


def mismatches(pyproject_text: str, lock_text: str) -> list[str]:
    """Each exact pin the lock does not carry at the same version, as one sentence."""
    lock = _pins([line for line in lock_text.splitlines() if not line.lstrip().startswith("#")])
    out = []
    for name, version in sorted(pinned(pyproject_text).items()):
        have = lock.get(name)
        if have != version:
            out.append(f"{name}: pyproject pins {version}, requirements-full.lock has {have or 'nothing'}")
    return out


def rebuild() -> int:
    with tempfile.TemporaryDirectory(prefix="observatory-lock-") as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        pip = [str(venv / "bin" / "python"), "-m", "pip"]
        subprocess.run([*pip, "install", "-q", "--upgrade", "pip"], check=True)
        subprocess.run([*pip, "install", "-q", f"{ROOT}[full]"], check=True)
        frozen = subprocess.run([*pip, "freeze", "--exclude", "project-observatory", "--exclude", "pip"],
                                check=True, capture_output=True, text=True).stdout
    lines = sorted((line for line in frozen.splitlines() if PIN.match(line)), key=lambda s: _norm(s.split("==")[0]))
    LOCK.write_text(HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    problems = mismatches((ROOT / "pyproject.toml").read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8"))
    for p in problems:
        print(p, file=sys.stderr)
    print(f"wrote {LOCK.name}: {len(lines)} packages" + ("" if not problems else f"; {len(problems)} pin(s) still differ"))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only compare the pins with the lock; no network")
    args = ap.parse_args(argv)
    if tomllib is None:
        print("refresh_lock needs Python 3.11 or newer (tomllib)", file=sys.stderr)
        return 2
    if args.check:
        problems = mismatches((ROOT / "pyproject.toml").read_text(encoding="utf-8"), LOCK.read_text(encoding="utf-8"))
        for p in problems:
            print(p, file=sys.stderr)
        if problems:
            print("requirements-full.lock is behind pyproject.toml: run `python tools/refresh_lock.py` "
                  "and commit the lock", file=sys.stderr)
            return 1
        print("requirements-full.lock carries every pin in pyproject.toml")
        return 0
    return rebuild()


if __name__ == "__main__":
    raise SystemExit(main())
