#!/usr/bin/env bash
# The tick is tools/tick.py (docs/design/WINDOWS-LINUX.md, W3). This wrapper keeps every
# scheduler entry that names tick.sh working, and picks the interpreter as it always has: the
# one that installed the engine (set by install_launchd.py), else a source checkout's venv,
# else python3. An installed package has no .venv, and a bare python3 lacks the [full]
# dependencies (sqlite-vec, mcp, jsonschema).
#
# An interpreter the installer NAMED (OBSERVATORY_PYTHON) that is gone is a stop, never a fallback:
# a package manager that removes the Python a virtual environment was made from leaves its
# `bin/python3` a dangling link, and the bare python3 this used to take instead ran ten ticks
# without the engine's dependencies and without a line saying why (2026-10-29). 78 is EX_CONFIG,
# the code launchd itself records for the jobs that start that interpreter directly; the line
# carries its UTC time like every other line in tick.err (log_policy.StampedLines).
cd "$(dirname "$0")/.." || exit 0
if [ -n "${OBSERVATORY_PYTHON:-}" ] && [ ! -x "$OBSERVATORY_PYTHON" ]; then
    made_from="$(sed -n 's/^home *= *//p' "$(dirname "$(dirname "$OBSERVATORY_PYTHON")")/pyvenv.cfg" 2>/dev/null | head -n 1)"
    if [ -n "$made_from" ]; then
        fix="its virtual environment was made from $made_from: reinstall that Python if it is gone, or recreate the environment and run \`install_launchd.py install\` again"
    else
        fix="reinstall the engine and run \`install_launchd.py install\` again"
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) tick not run: the engine's interpreter $OBSERVATORY_PYTHON is missing; $fix" >&2
    exit 78
fi
PY="${OBSERVATORY_PYTHON:-./.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)" || exit 0
exec "$PY" tools/tick.py "$@"
