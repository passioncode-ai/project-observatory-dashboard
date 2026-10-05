#!/usr/bin/env python3
"""The maintenance job's entry point: `maintain.py run|ensure|uninstall|status`.

launchd (org.project-observatory.<sha16>.maintain) and the systemd user timer run
`maintain.py run` every hour; the observatory-log plugin's SessionStart hook runs
`maintain.py hook`. Everything else is in maintenance.py."""
from __future__ import annotations
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import maintenance  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(maintenance.main(sys.argv[1:]))
