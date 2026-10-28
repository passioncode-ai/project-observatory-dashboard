"""Move one background job between two machines, so it never runs on both (OBS-283).

A second-machine restore (`full machine-restore`, machine_backup.py) writes every launchd plist
(systemd user unit off macOS) of the other machine into a staging folder that nothing loads at
login, and names each one in the placement file (machine_identity.py) as running elsewhere. A job
then moves one at a time, the old machine first:

    on the machine the job leaves:  project-observatory full machine-move leave LABEL --to MACHINE_ID
        stops the job there, moves its definition into that machine's staging folder (so the next
        login does not start it again) and names it in that machine's placement file as running on
        MACHINE_ID
    on the machine it moves to:     project-observatory full machine-move cutover LABEL
        moves the staged definition into the job folder, enables and loads it here, and removes it
        from this machine's placement file
    on either:                      project-observatory full machine-move show
        this machine's id (what `--to` takes), the placement file and the staged jobs

Nothing here reads the other machine: the person runs `leave` there, and `cutover` says so. A load
that fails puts the definition back into the staging folder, so a failed cutover never leaves a job
that would start at the next login. `OBSERVATORY_SYSTEM_SETUP=0` (a test, a sandbox) moves files
and writes the placement file but never calls launchctl or systemctl. The full checklist of a move
(both machines on one release, own SSH key, own device id, the vault equal) is OBS-286.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import machine_identity as mi

PROG = "project-observatory full machine-move"


class MachineMoveError(ValueError):
    """A job could not be moved. Never carries a value."""


class MachineMoveFailed(MachineMoveError):
    """A step failed after something changed; what was put back is in the message."""


def _system(platform: str) -> str:
    if platform == "darwin":
        return "launchd"
    if platform == "win32":
        raise MachineMoveError("moving a background job between machines is not available on Windows yet")
    return "systemd"


def job_folder(home: Path, system: str) -> Path:
    return home / "Library/LaunchAgents" if system == "launchd" else home / ".config/systemd/user"


def _definitions(folder: Path, system: str) -> list[Path]:
    if not folder.is_dir():
        return []
    pattern = "*.plist" if system == "launchd" else "*"
    return sorted(p for p in folder.glob(pattern)
                  if (p.is_file() or p.is_symlink()) and not p.name.startswith("."))


def _find(folder: Path, system: str, label: str) -> Path | None:
    for path in _definitions(folder, system):
        if (mi.job_label(path) if path.is_file() else path.stem) == label or path.name == label:
            return path
    return None


def _services(home: Path) -> dict[str, str]:
    import machine_backup
    folder = machine_backup._fabric_services_dir(home)
    return mi.service_ids_by_label(sorted(folder.glob("*.json")) if folder.is_dir() else [])


def _commands_allowed() -> bool:
    return os.environ.get("OBSERVATORY_SYSTEM_SETUP") != "0"


def _run(runner, argv: list[str]) -> subprocess.CompletedProcess:
    return runner(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def _loaded(runner, system: str, label: str, uid: int) -> bool:
    if system == "launchd":
        return _run(runner, ["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0
    return _run(runner, ["systemctl", "--user", "is-active", "--quiet", label]).returncode == 0


def _start(runner, system: str, label: str, definition: Path, uid: int) -> None:
    if system == "launchd":
        _run(runner, ["launchctl", "enable", f"gui/{uid}/{label}"])
        if _loaded(runner, system, label, uid):
            return
        p = _run(runner, ["launchctl", "bootstrap", f"gui/{uid}", str(definition)])
    else:
        _run(runner, ["systemctl", "--user", "daemon-reload"])
        p = _run(runner, ["systemctl", "--user", "enable", "--now", definition.name])
    if p.returncode != 0:
        tail = ((p.stderr or "").strip().splitlines() or [""])[-1][:200]
        raise MachineMoveError(f"{label} did not start here (exit {p.returncode}{': ' + tail if tail else ''})")


def _stop(runner, system: str, label: str, definition: Path, uid: int) -> None:
    if system == "launchd":
        if _loaded(runner, system, label, uid):
            p = _run(runner, ["launchctl", "bootout", f"gui/{uid}/{label}"])
            if p.returncode != 0 and _loaded(runner, system, label, uid):
                raise MachineMoveError(f"{label} is still running here; it was not moved")
        return
    p = _run(runner, ["systemctl", "--user", "disable", "--now", definition.name])
    if p.returncode != 0 and _loaded(runner, system, label, uid):
        raise MachineMoveError(f"{label} is still running here; it was not moved")


def show(home: Path, *, platform: str = sys.platform) -> dict:
    system = _system(platform)
    doc = mi.read_placement(home)
    staging = mi.staging_dir(home, system)
    return {"machine": mi.machine_id(home), "placement": str(mi.placement_path(home)),
            "elsewhere": doc["elsewhere"], "staging": str(staging),
            "staged": [{"label": mi.job_label(p) if p.is_file() else p.stem, "file": p.name}
                       for p in _definitions(staging, system)]}


def cutover(label: str, home: Path, *, runner=subprocess.run, platform: str = sys.platform,
            uid: int | None = None) -> dict:
    """Move one staged job here: into the job folder, enabled and loaded, and out of `elsewhere`."""
    if not mi.LABEL_SHAPE.fullmatch(label or ""):
        raise MachineMoveError("a label is letters, digits or . _ @ : -")
    system = _system(platform)
    uid = os.getuid() if uid is None else uid
    staging, folder = mi.staging_dir(home, system), job_folder(home, system)
    staged = _find(staging, system, label)
    live = _find(folder, system, label)
    me = mi.machine_id(home)
    if staged is None and live is None:
        raise MachineMoveError(f"no job {label} is staged here ({staging}) or defined here ({folder}); "
                               "`full machine-move show` lists the staged ones")
    if staged is not None:
        target = folder / staged.name
        if os.path.lexists(target):
            if target.is_file() and staged.is_file() and target.read_bytes() == staged.read_bytes():
                staged.unlink()
            else:
                raise MachineMoveError(f"{target} already exists and differs from the staged {staged.name}; "
                                       "move one of them aside first")
        else:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(target))
        live = target
    loaded = "skipped: OBSERVATORY_SYSTEM_SETUP=0"
    if _commands_allowed():
        try:
            _start(runner, system, label, live, uid)
            loaded = "loaded"
        except (MachineMoveError, OSError, subprocess.SubprocessError) as exc:
            mi.ensure_private_dir(staging, home)
            shutil.move(str(live), str(staging / live.name))
            raise MachineMoveFailed(f"{exc}; its definition is back in {staging}, so nothing starts it at login")
    was_elsewhere = mi.remove_elsewhere(label, home, machine=me)
    return {"label": label, "machine": me, "definition": str(live), "load": loaded,
            "was_elsewhere": was_elsewhere, "placement": str(mi.placement_path(home)),
            "next": [f"on the machine it left, if not done yet: project-observatory full machine-move leave {label} "
                     f"--to {me}  (stops it there, so it never runs on both)"]}


def leave(label: str, to: str, home: Path, *, runner=subprocess.run, platform: str = sys.platform,
          uid: int | None = None) -> dict:
    """Stop one job here, stage its definition and name it as running on machine `to`."""
    if not mi.LABEL_SHAPE.fullmatch(label or ""):
        raise MachineMoveError("a label is letters, digits or . _ @ : -")
    if not mi.valid_id(to):
        raise MachineMoveError("--to takes the other machine's id (`full machine-move show` prints it there)")
    system = _system(platform)
    uid = os.getuid() if uid is None else uid
    me = mi.machine_id(home)
    if to == me:
        raise MachineMoveError("--to names this machine; run `leave` on the machine the job leaves")
    staging, folder = mi.staging_dir(home, system), job_folder(home, system)
    live = _find(folder, system, label)
    stopped = "skipped: OBSERVATORY_SYSTEM_SETUP=0"
    if live is not None:
        if _commands_allowed():
            _stop(runner, system, label, live, uid)
            stopped = "stopped"
        mi.ensure_private_dir(staging, home)
        target = staging / live.name
        if os.path.lexists(target):
            target.unlink()
        shutil.move(str(live), str(target))
    elif _find(staging, system, label) is None:
        raise MachineMoveError(f"no job {label} is defined here ({folder}) or staged here ({staging})")
    else:
        stopped = "already staged"
    path = mi.place_elsewhere([{"label": label, "serviceId": _services(home).get(label), "placedOn": to,
                                "by": "machine-move leave"}], home, machine=me)
    return {"label": label, "machine": me, "placed_on": to, "stop": stopped, "staging": str(staging),
            "placement": str(path),
            "next": [f"on {to}: project-observatory full machine-move cutover {label}"]}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description="move one background job between two machines, so it "
                                                        "never runs on both (the placement file says where it runs)")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="this machine's id, the placement file and the jobs staged here")
    cut = sub.add_parser("cutover", help="start a staged job here and remove it from this machine's `elsewhere`")
    cut.add_argument("label")
    out = sub.add_parser("leave", help="stop a job here, stage it, and name it as running on another machine")
    out.add_argument("label")
    out.add_argument("--to", required=True, metavar="MACHINE_ID", help="the id `machine-move show` prints there")
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    home = Path.home()
    try:
        if a.action == "show":
            result = show(home)
        elif a.action == "cutover":
            result = cutover(a.label, home)
        else:
            result = leave(a.label, a.to, home)
    except (MachineMoveError, mi.MachineIdentityError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, (OSError, MachineMoveFailed)) else 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
