"""This machine's stable id, and the placement file every product reads (OBS-283, OBS-284).

WHY. A machine bundle and a workspace snapshot are taken on one machine and may be restored on
another. Restored there as they are, they would give the second machine the first one's SSH key,
load every launchd job at the next login (a bot polling one token on two machines, a scheduler
acting twice) and copy the agent-network device identity. So a bundle and a snapshot now name the
machine they were taken on, and a restore compares that id with this machine's.

THE MACHINE ID is not a key and proves nothing: it only tells two machines apart. It is
`m-` and 24 hex digits of SHA-256 over the platform's own stable machine identifier (macOS
`IOPlatformUUID`, Linux `/etc/machine-id`, Windows `MachineGuid`), so the identifier itself is
never written anywhere. Where none can be read, a random id is kept once in
`${XDG_DATA_HOME:-~/.local/share}/project-observatory/machine-id`, a folder neither the machine
bundle nor the workspace snapshot carries. `OBSERVATORY_MACHINE_ID` overrides it (tests, and a
person who wants to name a machine). It is never the device key of `full node --init`.

THE PLACEMENT FILE, `${XDG_DATA_HOME:-~/.local/share}/project-observatory/placement.json`, schema
`observatory-placement/1`, says which background jobs this machine holds for another one until the
estate record carries placement:

    {"schema": "observatory-placement/1", "machine": "<this machine's id>", "updatedAt": "<UTC>",
     "elsewhere": [{"label": "<launchd label or systemd unit>", "serviceId": "<id.instance>" | null,
                    "placedOn": "<the machine id it runs on>", "since": "<UTC>", "by": "<command>"}]}

Owner-only (0600 in a 0700 folder), written atomically. Fabric Dashboards (FD-132) reads it to show
an agent as placed on another machine and hide its start. `full machine-restore` on a second machine
adds every job it stages; `full machine-move cutover LABEL` removes one when the job moves here, and
`full machine-move leave LABEL --to MACHINE` adds one when it moves away.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import subprocess
import sys
import tempfile
import xml.parsers.expat

SCHEMA = "observatory-placement/1"
ENV = "OBSERVATORY_MACHINE_ID"
PREFIX = "observatory-machine-id/1\0"
#: What a machine id looks like when it is read from a file, a manifest or an argument.
ID_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
#: A launchd label or a systemd unit name, as the placement file and `machine-move` take it.
LABEL_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,254}")
FOLDER = "project-observatory"
PLACEMENT_NAME = "placement.json"
ID_FILE = "machine-id"
#: Where a second-machine restore puts the jobs it must not load, per job system.
STAGED = {"launchd": "LaunchAgents", "systemd": "systemd-user"}


class MachineIdentityError(ValueError):
    """The machine id or the placement file could not be read or written. Never carries a value."""


def data_dir(home: Path | None = None) -> Path:
    """`${XDG_DATA_HOME:-<home>/.local/share}/project-observatory`, on every platform."""
    value = os.environ.get("XDG_DATA_HOME")
    base = Path(value) if value and Path(value).is_absolute() else (home or Path.home()) / ".local" / "share"
    return base / FOLDER


def placement_path(home: Path | None = None) -> Path:
    return data_dir(home) / PLACEMENT_NAME


def staging_dir(home: Path | None = None, system: str = "launchd") -> Path:
    """The folder a second-machine restore writes `system`'s jobs to: nothing loads it at login."""
    return data_dir(home) / "machine-move" / STAGED[system]


def _platform_identifier(runner=subprocess.run) -> str | None:
    """The platform's own stable machine identifier, or None. Read, hashed by the caller, never stored."""
    if sys.platform == "darwin":
        try:
            p = runner(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                       text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{16,64})"', p.stdout or "")
        return found.group(1).upper() if found else None
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip() or None
        except (OSError, ImportError):
            return None
    for name in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(name).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if re.fullmatch(r"[0-9a-f]{32}", value):
            return value
    return None


def ensure_private_dir(path: Path, home: Path | None = None) -> Path:
    """Create `path` and make it and every folder between it and the data folder owner-only."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    top = data_dir(home)
    chain = [path, *path.parents]
    for folder in chain:
        if os.name != "nt":
            os.chmod(folder, 0o700)
        if folder == top or top not in folder.parents:
            break
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Atomic, owner-only: a 0600 file in a 0700 folder, fsynced, then renamed into place."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def machine_id(home: Path | None = None, *, runner=subprocess.run) -> str:
    """This machine's id: `OBSERVATORY_MACHINE_ID`, else a hash of the platform's identifier,
    else a random id kept once in the data folder. Stable for the life of the machine."""
    override = os.environ.get(ENV)
    if override:
        if not ID_SHAPE.fullmatch(override):
            raise MachineIdentityError(f"{ENV} must be 8 to 128 letters, digits or . _ : -")
        return override
    raw = _platform_identifier(runner)
    if raw:
        return "m-" + hashlib.sha256((PREFIX + raw).encode()).hexdigest()[:24]
    kept = data_dir(home) / ID_FILE
    try:
        value = kept.read_text(encoding="ascii").strip()
        if ID_SHAPE.fullmatch(value):
            return value
    except (OSError, UnicodeDecodeError):
        pass
    value = "m-" + secrets.token_hex(12)
    _write_private(kept, (value + "\n").encode())
    return value


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(ID_SHAPE.fullmatch(value))


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_placement(home: Path | None = None) -> dict:
    """The placement file, or an empty one for this machine when there is none yet. A file that
    is not a placement document refuses, by its path, rather than being silently replaced."""
    path = placement_path(home)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "machine": None, "updatedAt": None, "elsewhere": []}
    except (OSError, ValueError) as exc:
        raise MachineIdentityError(f"{path} is not readable JSON ({type(exc).__name__}); move it aside") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("elsewhere"), list):
        raise MachineIdentityError(f"{path} is not an {SCHEMA} document; move it aside")
    doc["elsewhere"] = [e for e in doc["elsewhere"] if isinstance(e, dict) and isinstance(e.get("label"), str)]
    return doc


def write_placement(doc: dict, home: Path | None = None, *, machine: str | None = None) -> Path:
    path = placement_path(home)
    out = {"schema": SCHEMA, "machine": machine or doc.get("machine") or machine_id(home),
           "updatedAt": now(),
           "elsewhere": sorted(doc.get("elsewhere") or [], key=lambda e: e["label"])}
    _write_private(path, (json.dumps(out, indent=2, ensure_ascii=False) + "\n").encode())
    return path


def place_elsewhere(entries: list[dict], home: Path | None = None, *, machine: str | None = None) -> Path:
    """Add or replace `entries` (by label) in this machine's `elsewhere`."""
    doc = read_placement(home)
    by_label = {e["label"]: e for e in doc["elsewhere"]}
    for entry in entries:
        if not LABEL_SHAPE.fullmatch(str(entry.get("label", ""))):
            raise MachineIdentityError("a label must be letters, digits or . _ @ : -")
        by_label[entry["label"]] = {"label": entry["label"], "serviceId": entry.get("serviceId"),
                                    "placedOn": entry.get("placedOn"), "since": entry.get("since") or now(),
                                    "by": entry.get("by")}
    doc["elsewhere"] = list(by_label.values())
    return write_placement(doc, home, machine=machine)


def remove_elsewhere(label: str, home: Path | None = None, *, machine: str | None = None) -> bool:
    """Remove `label` from this machine's `elsewhere`; whether it was there."""
    doc = read_placement(home)
    kept = [e for e in doc["elsewhere"] if e["label"] != label]
    found = len(kept) != len(doc["elsewhere"])
    doc["elsewhere"] = kept
    write_placement(doc, home, machine=machine)
    return found


def service_ids_by_label(descriptors: list[Path]) -> dict[str, str]:
    """`{launchd label or systemd unit: "<id>.<instance>"}` from Fabric service descriptors
    (`fabric-service/0.1`, `lifecycle.label`). A descriptor that does not read is skipped."""
    out: dict[str, str] = {}
    for file in descriptors:
        try:
            doc = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        life = doc.get("lifecycle") if isinstance(doc.get("lifecycle"), dict) else {}
        label, sid, inst = life.get("label"), doc.get("id"), doc.get("instance")
        if isinstance(label, str) and isinstance(sid, str) and isinstance(inst, str):
            out.setdefault(label, f"{sid}.{inst}")
    return out


def job_label(path: Path, data: bytes | None = None) -> str:
    """A launchd plist's `Label`, else the file's stem; a systemd unit's file name."""
    if path.suffix == ".plist":
        try:
            doc = plistlib.loads(data if data is not None else path.read_bytes())
            if isinstance(doc, dict) and isinstance(doc.get("Label"), str) and LABEL_SHAPE.fullmatch(doc["Label"]):
                return doc["Label"]
        except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
            pass
        return path.stem
    return path.name


def restore_mode(recorded, *, asked: str | None = None, here: str | None = None,
                 home: Path | None = None) -> dict:
    """Whether a bundle or snapshot taken on machine `recorded` is restored here as the same machine.

    `asked` is the person's choice: `second` (always safe) or `same` (a terminal-only override the
    caller checks). Otherwise the ids decide, and a bundle or snapshot that names no machine (taken
    before 0.30.1) is treated as another machine's: the safe reading of an unknown."""
    if asked not in (None, "second", "same"):
        raise MachineIdentityError("the restore mode is `second` or `same`")
    here = here or machine_id(home)
    recorded = recorded if valid_id(recorded) else None
    if asked:
        why = "asked"
        same = asked == "same"
    elif recorded is None:
        why, same = "names-no-machine", False
    else:
        same = recorded == here
        why = "same-machine" if same else "another-machine"
    return {"mode": "same-machine" if same else "second-machine", "why": why,
            "this_machine": here, "taken_on": recorded}
