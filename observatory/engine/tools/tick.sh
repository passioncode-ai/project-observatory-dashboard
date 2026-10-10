#!/usr/bin/env bash
# The tick is tools/tick.py (docs/design/WINDOWS-LINUX.md, W3). This wrapper keeps every
# scheduler entry that names tick.sh working, and picks the interpreter as it always has: the
# one that installed the engine (set by install_launchd.py), else a source checkout's venv,
# else python3. An installed package has no .venv, and a bare python3 lacks the [full]
# dependencies (sqlite-vec, mcp, jsonschema).
cd "$(dirname "$0")/.." || exit 0
PY="${OBSERVATORY_PYTHON:-./.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)" || exit 0
exec "$PY" tools/tick.py "$@"
