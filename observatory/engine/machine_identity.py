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
bundle nor the workspace snapshot carries. It is never the device key of `full node --init`.
`OBSERVATORY_MACHINE_ID` overrides it only in a sandbox (`OBSERVATORY_SYSTEM_SETUP=0`, which the
test runner sets, or `OBSERVATORY_MACHINE_ID_FOR_TESTS=1`, which a test sets): honoured anywhere, a variable could make a bundle from another machine read as
this one's and restore its private SSH keys here. Two machines cloned from one disk image or
virtual machine share the platform identifier and so the id: give a clone its own identifier
(`systemd-machine-id-setup` on Linux, a new virtual machine UUID in the hypervisor) before restoring.

THE PLACEMENT FILE, `${XDG_DATA_HOME:-~/.local/share}/project-observatory/placement.json`, schema
`observatory-placement/1`, says which background jobs this machine holds for another one until the
estate record carries placement:

    {"schema": "observatory-placement/1", "machine": "<this machine's id>", "updatedAt": "<UTC>",
     "elsewhere": [{"label": "<launchd label or systemd unit>", "serviceId": "<id.instance>" | null,
                    "placedOn": "<the machine id it runs on>", "since": "<UTC>", "by": "<command>",
                    "forward": "<launchd label>", "forwardAdopted": false}]}

`forward` and `forwardAdopted` (OBS-480, optional, absent when no forward is set up) name the launchd job that
forwards the service's local port to the space where it now runs (machine_forward.py), and whether it was
set up by hand and adopted. The schema id stays `observatory-placement/1`: a reader that knows only the
first five keys reads every entry as before.

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
import contextlib

import oslocks
import osprivacy

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
#: Where `machine-move cutover` puts a launchd job of an account with no GUI session (OBS-361): an
#: administrator's `space_daemons.py apply` loads it from there as a system daemon run as this account.
BACKGROUND = ("background", "LaunchAgents")


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


def background_dir(home: Path | None = None) -> Path:
    """The folder of the launchd jobs this account runs as system daemons, having no GUI session (OBS-361).
    `tools/space_daemons.py` mirrors it as root sees it, without XDG_DATA_HOME."""
    return data_dir(home).joinpath(*BACKGROUND)


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


TEST_MARKER = "OBSERVATORY_MACHINE_ID_FOR_TESTS"


def machine_id(home: Path | None = None, *, runner=subprocess.run, create: bool = True) -> str | None:
    """This machine's id: `OBSERVATORY_MACHINE_ID` in a sandbox or a test, else a hash of the platform's identifier,
    else a random id kept once in the data folder. Stable for the life of the machine. With `create=False` a
    machine with no platform identifier and no kept id answers None instead of writing one (a read-only
    caller such as `full machine-move status`)."""
    # Only a sandbox or a test names the machine: a variable carried in a restored shell profile must
    # never make a second machine read as the first (review of #394), so the override alone is ignored.
    testing = os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0" or os.environ.get(TEST_MARKER) == "1"
    override = os.environ.get(ENV) if testing else None
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
    if not create:
        return None
    value = "m-" + secrets.token_hex(12)
    _write_private(kept, (value + "\n").encode())
    return value


def valid_id(value) -> bool:
    return isinstance(value, str) and bool(ID_SHAPE.fullmatch(value))


# --- the place: this host and this OS account (OBS-357) ---------------------------------------------
#
# Two OS accounts of one host share the host id, so an id that names only the host cannot tell this
# account's bundle, placement or receipt from another account's. The place id is the host id with a
# hash of the account appended: `<host id>.a-<10 hex>`, 39 characters, inside ID_SHAPE, so every reader
# that checks the shape accepts it. It names no account, and two accounts of one host share its prefix.

PLACE_PREFIX = "observatory-place-id/1\0"
ACCOUNT_ENV = "OBSERVATORY_ACCOUNT_FOR_TESTS"
_PLACE = re.compile(r"(?P<host>[A-Za-z0-9][A-Za-z0-9._:-]{7,116})\.a-(?P<account>[0-9a-f]{10})")


def _account() -> str:
    """This OS account as text (the uid, the SID on Windows). A sandbox or a test may fake it, under the rule
    that guards OBSERVATORY_MACHINE_ID."""
    testing = os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0" or os.environ.get(TEST_MARKER) == "1"
    if testing and os.environ.get(ACCOUNT_ENV):
        return os.environ[ACCOUNT_ENV]
    import osprivacy
    return osprivacy.account()


def is_place(value) -> bool:
    return valid_id(value) and bool(_PLACE.fullmatch(value))


def host_of(value) -> str | None:
    """The host part of a place id, or a bare host id itself; None for what is not an id."""
    if not valid_id(value):
        return None
    m = _PLACE.fullmatch(value)
    return m.group("host") if m else value


def place_id(home: Path | None = None, *, runner=subprocess.run, create: bool = True) -> str | None:
    """This place: the host id and this OS account. None when the host id is None (`create=False`)."""
    host = machine_id(home, runner=runner, create=create)
    if host is None:
        return None
    if is_place(host):                                   # a sandbox's override that already names a place
        return host
    digest = hashlib.sha256((PLACE_PREFIX + host + "\0" + _account()).encode()).hexdigest()[:10]
    return f"{host}.a-{digest}"


def match(recorded, here: str | None = None, *, accounts=...) -> str:
    """How an id written somewhere compares with this place: `same`, `other`, or `same-host-account-unknown`
    (a bare host id of this host, written by an engine before place ids, on a host several accounts share or whose
    account count cannot be told). A bare id of this host on a one-account host is `same`, as before."""
    here = here or place_id(create=False)
    if not valid_id(recorded) or not here:
        return "other"
    if is_place(recorded):
        return "same" if recorded == here else "other"
    if recorded != host_of(here):
        return "other"
    if accounts is ...:                                  # not given: read the host (None is "cannot be told")
        accounts = accounts_on_host()
    return "same" if accounts == 1 else "same-host-account-unknown"


def accounts_on_host() -> int | None:
    """People's accounts on this host. A sandbox is a one-account host unless OBSERVATORY_HOST_ACCOUNTS says
    otherwise, so a suite's verdicts never depend on the CI runner's accounts."""
    testing = os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0" or os.environ.get(TEST_MARKER) == "1"
    if testing:
        told = os.environ.get("OBSERVATORY_HOST_ACCOUNTS", "1")
        return int(told) if told.isdigit() else None
    import host_accounts
    return host_accounts.count()


def place_summary(home: Path | None = None) -> dict:
    """Doctor's `place` block: the host, this place and how many accounts share the host."""
    try:
        place = place_id(home, create=False)
    except MachineIdentityError as exc:
        return {"error": str(exc)[:200]}
    return {"host": host_of(place), "place": place, "accountsOnHost": accounts_on_host()}


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


@contextlib.contextmanager
def _placement_lock(home: Path | None):
    """One writer of the placement file at a time: `machine-restore`, `machine-move` and a second
    terminal would otherwise each read, change and write it and lose the other's change."""
    lock = data_dir(home) / ".placement.lock"
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(lock, os.O_RDWR | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
    try:
        oslocks.flock(fd, oslocks.LOCK_EX)
        yield
    finally:
        os.close(fd)


def place_elsewhere(entries: list[dict], home: Path | None = None, *, machine: str | None = None) -> Path:
    """Add or replace `entries` (by label) in this machine's `elsewhere`."""
    with _placement_lock(home):
        return _place_elsewhere(entries, home, machine)


def _place_elsewhere(entries: list[dict], home: Path | None, machine: str | None) -> Path:
    doc = read_placement(home)
    by_label = {e["label"]: e for e in doc["elsewhere"]}
    for entry in entries:
        if not LABEL_SHAPE.fullmatch(str(entry.get("label", ""))):
            raise MachineIdentityError("a label must be letters, digits or . _ @ : -")
        kept = by_label.get(entry["label"]) or {}
        row = {"label": entry["label"], "serviceId": entry.get("serviceId"),
               "placedOn": entry.get("placedOn"), "since": entry.get("since") or now(), "by": entry.get("by")}
        # OBS-480: an entry that does not name `forward` keeps the forward already recorded for its label (a
        # restore placing it again must not forget the job that forwards its port); `forward: None` clears it.
        forward = entry["forward"] if "forward" in entry else kept.get("forward")
        if forward is not None:
            row["forward"] = forward
            row["forwardAdopted"] = (entry.get("forwardAdopted") if "forward" in entry
                                     else kept.get("forwardAdopted")) is True
        by_label[entry["label"]] = row
    doc["elsewhere"] = list(by_label.values())
    return write_placement(doc, home, machine=machine)


def set_forward(label: str, home: Path | None = None, *, forward: str | None, adopted: bool = False,
                machine: str | None = None) -> bool:
    """Record (or with `forward=None` clear) the launchd job that forwards LABEL's port to where it runs
    (OBS-480) in its `elsewhere` entry, keeping every other field; whether LABEL is placed elsewhere."""
    if forward is not None and not LABEL_SHAPE.fullmatch(forward):
        raise MachineIdentityError("a forward is a launchd label: letters, digits or . _ @ : -")
    with _placement_lock(home):
        doc = read_placement(home)
        found = False
        for entry in doc["elsewhere"]:
            if entry["label"] == label:
                found = True
                entry.pop("forward", None)
                entry.pop("forwardAdopted", None)
                if forward is not None:
                    entry["forward"], entry["forwardAdopted"] = forward, bool(adopted)
        if found:
            write_placement(doc, home, machine=machine)
        return found


def remove_elsewhere(label: str, home: Path | None = None, *, machine: str | None = None) -> bool:
    """Remove `label` from this machine's `elsewhere`; whether it was there."""
    with _placement_lock(home):
        doc = read_placement(home)
        kept = [e for e in doc["elsewhere"] if e["label"] != label]
        found = len(kept) != len(doc["elsewhere"])
        doc["elsewhere"] = kept
        write_placement(doc, home, machine=machine)
        return found


RECEIPT_SCHEMA = "observatory-machine-restore/1"
RECEIPT_NAME = "restore.json"
#: The parts of a restore receipt, one per step that writes; a later run of a step replaces its part.
RECEIPT_PARTS = ("apply", "packages", "clone")


def restore_receipt_path(home: Path | None = None) -> Path:
    """`<data folder>/machine-move/restore.json`: what the last `full machine-restore` wrote here (OBS-286)."""
    return data_dir(home) / "machine-move" / RECEIPT_NAME


def read_restore_receipt(home: Path | None = None) -> dict | None:
    """The restore receipt, or None when no restore wrote here. One that does not read refuses by its path."""
    path = restore_receipt_path(home)
    try:
        doc = json.loads(osprivacy.read_regular(path, 1 << 20).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise MachineIdentityError(f"{path} is not readable JSON ({type(exc).__name__}); move it aside") from None
    if not isinstance(doc, dict) or doc.get("schema") != RECEIPT_SCHEMA:
        raise MachineIdentityError(f"{path} is not an {RECEIPT_SCHEMA} document; move it aside")
    return doc


def record_restore(parts: dict, home: Path | None = None) -> Path:
    """Write `parts` (`apply`, `packages`, `clone`) into the restore receipt, each stamped with the time,
    keeping the parts this run did not touch. Owner-only and atomic, under the placement file's lock.
    It holds bundle names, counts, machine ids, categories and public key fingerprints: never a value."""
    with _placement_lock(home):
        try:
            doc = read_restore_receipt(home) or {}
        except MachineIdentityError:
            doc = {}
        out = {"schema": RECEIPT_SCHEMA, "machine": place_id(home), "updatedAt": now()}
        for name in RECEIPT_PARTS:
            if name in parts:
                out[name] = {"at": now(), **parts[name]}
            elif isinstance(doc.get(name), dict):
                out[name] = doc[name]
        path = restore_receipt_path(home)
        ensure_private_dir(path.parent, home)
        _write_private(path, (json.dumps(out, indent=2, ensure_ascii=False) + "\n").encode())
        return path


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
                 home: Path | None = None, unknown: str = "second", home_path: str | None = None) -> dict:
    """Whether a bundle or snapshot taken at place `recorded` is restored here as the same machine.

    `asked` is the person's choice: `second` (always safe) or `same` (a terminal-only override the
    caller checks). Otherwise `match` decides (OBS-357): this very place is the same machine; another account
    of this host, or another host, is a second machine. An id an engine wrote before place ids names only the
    host: on a host with one account it is this place, as before; on a host several accounts share, a bundle
    whose `home_path` is this home is this account's (`same-host-same-home`), anything else is a second machine.
    A bundle or snapshot that names no machine (taken before 0.30.1) is another machine's, the safe reading of
    an unknown; `unknown="keep"` is `full init`'s reading on an empty home, where such a snapshot is most often
    this machine's own (taken before an update), restored whole with the answer `unknown`."""
    if asked not in (None, "second", "same"):
        raise MachineIdentityError("the restore mode is `second` or `same`")
    here = here or place_id(home)
    recorded = recorded if valid_id(recorded) else None
    if asked:
        why = "asked"
        same = asked == "same"
    elif recorded is None:
        why, same = "names-no-machine", unknown == "keep"
    else:
        verdict = match(recorded, here)
        if verdict == "same":
            why, same = "same-place", True
        elif verdict == "same-host-account-unknown":
            same = bool(home_path) and Path(home_path) == (home or Path.home())
            why = "same-host-same-home" if same else "same-host-account-unknown"
        else:
            same = False
            why = "another-account" if host_of(recorded) == host_of(here) else "another-machine"
    mode = "unknown" if why == "names-no-machine" and same else ("same-machine" if same else "second-machine")
    return {"mode": mode, "why": why, "this_machine": here, "taken_on": recorded,
            "this_host": host_of(here), "taken_on_host": host_of(recorded) if recorded else None,
            "same_host": bool(recorded) and host_of(recorded) == host_of(here)}