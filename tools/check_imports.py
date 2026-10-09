#!/usr/bin/env python3
"""Import every library module of the engine and say which fail, and why (docs/design/WINDOWS-LINUX.md, W0).

On a new OS the first question is not whether a feature works but whether the code loads: one
`import fcntl` at the top of `workspace.py` was enough to stop the whole engine on Windows. This
loads each LIBRARY module, each in a fresh interpreter — the engine's root modules and the `store`,
`mcp`, `dashboard`, `agent` folders — never `collectors/` or `tools/`, whose modules do their work at import time
(a scan, a write) and are run as scripts. It prints one JSON document and exits 1 when any fails.

    python tools/check_imports.py            # from the repository root
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "observatory" / "engine"
# These folders are not packages: each module is loaded as the engine loads it, with its own
# folder and the engine root first on the path (`mcp/` would otherwise be the `mcp` library).
FOLDERS = ("store", "mcp", "dashboard", "agent")
LOADER = """
import importlib.util, json, sys, traceback
path, folder, engine = sys.argv[1:4]
sys.path[:0] = [folder, engine]
try:
    name = path.rsplit("/", 1)[-1].rsplit(chr(92), 1)[-1][:-3]
    spec = importlib.util.spec_from_file_location(name, path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except BaseException as exc:
    tb = traceback.extract_tb(exc.__traceback__)
    print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                      "at": f"{tb[-1].filename.rsplit(chr(47), 1)[-1].rsplit(chr(92), 1)[-1]}:{tb[-1].lineno}" if tb else ""}))
    sys.exit(1)
"""


def modules() -> list[Path]:
    files = sorted(p for p in ENGINE.glob("*.py") if p.stem != "__init__")
    for folder in FOLDERS:
        files += sorted((ENGINE / folder).glob("*.py"))
    return files


def check(path: Path, env: dict) -> dict | None:
    proc = subprocess.run([sys.executable, "-c", LOADER, str(path), str(path.parent), str(ENGINE)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=120)
    if proc.returncode == 0:
        return None
    lines = proc.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"error": f"exit {proc.returncode}: {proc.stderr.strip()[-200:]}", "at": ""}


def main() -> int:
    # A sandboxed home: importing must not read or create the user's workspace.
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "observatory-import-check"
    env = dict(os.environ, OBSERVATORY_HOME=str(base / "workspace"), PYTHONIOENCODING="utf-8")
    files = modules()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(files, pool.map(lambda f: check(f, env), files)))
    failed = {f.relative_to(ENGINE).as_posix(): r for f, r in results.items() if r}
    print(json.dumps({"platform": sys.platform, "python": sys.version.split()[0], "modules": len(files),
                      "failed": len(failed), "failures": failed}, indent=1, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
