#!/usr/bin/env python3
"""The start of a background job under Windows Task Scheduler (docs/design/WINDOWS-LINUX.md, W4).

A scheduled task names one program and its arguments, nothing else: no environment, no log files,
and a task started by `python.exe` opens a console window on the person's desktop each time. So a
task `osschedule.TaskSchedulerJob` creates runs this wrapper under `pythonw.exe`:

    pythonw tools/scheduled.py --cwd DIR --stdout LOG --stderr LOG [--env NAME=VALUE]… -- PYTHON SCRIPT ARGS…

It sets the environment and the working folder, appends the job's output to its two logs (bytes,
unchanged), runs the job without a window and exits with the job's own code, so the task's "Last
Result" is the job's. No value passed here is a secret: a task definition is readable by other
local tools, as a launchd plist is.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
import osprivacy  # noqa: E402

#: CREATE_NO_WINDOW: the job and every process it starts get no console of their own.
NO_WINDOW = 0x08000000


def parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" not in argv:
        raise SystemExit("scheduled.py: the job's command goes after --")
    cut = argv.index("--")
    parser = argparse.ArgumentParser(prog="scheduled.py")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("--env", action="append", default=[])
    args = parser.parse_args(argv[:cut])
    command = argv[cut + 1:]
    if not command:
        raise SystemExit("scheduled.py: no command after --")
    return args, command


def log(path: str) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return osprivacy.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)


def main(argv: list[str]) -> int:
    args, command = parse(argv)
    env = dict(os.environ)
    for entry in args.env:
        name, sep, value = entry.partition("=")
        if not sep or not name:
            raise SystemExit(f"scheduled.py: --env wants NAME=VALUE, not {entry!r}")
        env[name] = value
    out, err = log(args.stdout), log(args.stderr)
    try:
        flags = NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(command, cwd=args.cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=out, stderr=err, creationflags=flags).returncode
    except OSError as exc:
        os.write(err, f"scheduled.py: could not start {command[0]}: {exc}\n".encode())
        return 127
    finally:
        os.close(out)
        os.close(err)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
