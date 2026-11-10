"""A port forward for a service moved to another machine (OBS-480).

WHY. A Fabric service whose descriptor's origin is a local port (`http://127.0.0.1:PORT`) keeps being
read at that port on this machine after `full machine-move leave` moved it to another one: Fabric
Dashboards, the agents that call it and the descriptor still name this machine's port. Until now a
person set up one launchd job per service by hand that ran `ssh -N -L 127.0.0.1:PORT:127.0.0.1:PORT
USER@HOST` with KeepAlive. `leave --via USER@HOST` now installs that job itself, `return` removes it,
and the `elsewhere` entry of the placement file (machine_identity.py) names it in `forward`, so Fabric
Dashboards (FD-196) can tell a forward that is not running from one that was never set up.

WHERE THE DESTINATION COMES FROM. The data model has no ssh destination for a machine: a machine id is a
hash that proves nothing, and one machine may hold several spaces (OS accounts), each running other
services. So the person or agent names the space the service now runs in with `--via USER@HOST` on each
`leave`, HOST being a DNS name, an IPv4 address or a Host alias of ~/.ssh/config. USER is required: the
space is an account. The value is checked strictly (letters, digits, `.`, `_`, `-`; no option, no port,
no shell word) and only ever reaches ssh as the last element of an argument array, after `--`.

THE JOB. `org.project-observatory.forward.<service id>.<instance>`, in ~/Library/LaunchAgents, owner-only
(0600), with RunAtLoad, KeepAlive and a 30 s ThrottleInterval, running

    /usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
                 -o ServerAliveCountMax=3 -L 127.0.0.1:PORT:127.0.0.1:PORT -- USER@HOST

directly (no shell). The local end binds the loopback address only; BatchMode means it never prompts
(the space's host key must already be known and a key must sign in: ssh there once by hand first);
ExitOnForwardFailure makes a forward that cannot bind its port exit, so launchd's state says it is not
running instead of an ssh that runs and forwards nothing. The engine's own label prefix keeps the job out
of a machine bundle (each machine has its own forwards) and out of `machine-move status` M4.

ADOPTION. A launchd job already in ~/Library/LaunchAgents whose ProgramArguments are an `ssh` with
`-L [127.0.0.1|localhost:]PORT:...` to the same USER and HOST is adopted: recorded in `forward` with
`forwardAdopted: true`, never installed a second time, and never unloaded or removed by `return` unless
`--remove-adopted` asks for it (then it is unloaded and its plist set aside, never deleted). A job that
forwards the port to another destination, and a port held by anything that is not such a job, refuse the
forward by name; the move of the service itself is done by then and stays done.

Off macOS a forward is refused before anything moves: a systemd user unit for it is not written yet.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
import xml.parsers.expat

import machine_identity as mi

#: The forward jobs this engine installs; only a label with this prefix is ever removed by `return`.
PREFIX = "org.project-observatory.forward."
SSH = "/usr/bin/ssh"
USER_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,31}")
_LABEL_PART = r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
HOST_SHAPE = re.compile(rf"{_LABEL_PART}(?:\.{_LABEL_PART})*")
ORIGIN = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")
#: ssh's options that take an argument (OpenSSH 9, `ssh -h`): their value is never the destination.
ARG_OPTIONS = set("BbcDEeFIiJLlmOoPpQRSWw")
LOOPBACK = ("127.0.0.1", "localhost")
#: How long the port check waits for a service that was just stopped to let its port go.
PORT_WAIT_SECONDS = 5.0


class ForwardError(ValueError):
    """A forward could not be set up as asked. Never carries a value."""


def macos_only() -> str:
    return ("a port forward is a launchd job, macOS only: a systemd user unit for it is not written yet "
            "(leave without --via moves the job as before)")


def parse_via(text) -> tuple[str, str]:
    """(user, host) of `USER@HOST`, or ForwardError naming the rule."""
    if not isinstance(text, str) or text.count("@") != 1:
        raise ForwardError("--via takes USER@HOST: the account the service now runs as, and its machine")
    user, host = text.split("@")
    if not USER_SHAPE.fullmatch(user):
        raise ForwardError("--via: USER is letters, digits or . _ - (not starting with a digit, . or -)")
    if len(host) > 253 or not HOST_SHAPE.fullmatch(host):
        raise ForwardError("--via: HOST is a DNS name, an IPv4 address or a Host alias of ~/.ssh/config "
                           "(letters, digits, - and dots; no port, no option, no IPv6 literal)")
    return user, host


def forward_label(service_id: str) -> str:
    label = PREFIX + service_id
    if not mi.LABEL_SHAPE.fullmatch(label):
        raise ForwardError("the service id does not make a launchd label")
    return label


def plist_path(home: Path, label: str) -> Path:
    return home / "Library/LaunchAgents" / f"{label}.plist"


def service_of(home: Path, job_label: str) -> dict | None:
    """The Fabric service whose descriptor names launchd label JOB_LABEL: `{serviceId, port}`, port None for
    an origin that is not a local loopback port (a remote placement, another shape)."""
    import machine_backup
    folder = machine_backup._fabric_services_dir(home)
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    for file in files:
        try:
            doc = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        life = doc.get("lifecycle") if isinstance(doc.get("lifecycle"), dict) else {}
        sid, inst = doc.get("id"), doc.get("instance")
        if life.get("label") != job_label or not isinstance(sid, str) or not isinstance(inst, str):
            continue
        match = ORIGIN.fullmatch(str(doc.get("origin") or ""))
        port = int(match.group(1)) if match and doc.get("placement", "local") == "local" else None
        if port is not None and not 1024 <= port <= 65535:
            port = None
        return {"serviceId": f"{sid}.{inst}", "port": port}
    return None


def forward_document(label: str, port: int, user: str, host: str, ssh: str = SSH) -> dict:
    return {"Label": label,
            "ProgramArguments": [ssh, "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                                 "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                                 "-L", f"127.0.0.1:{port}:127.0.0.1:{port}", "--", f"{user}@{host}"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Background"}


def ssh_forward(doc) -> dict | None:
    """`{ports, user, host}` of a launchd job that runs ssh with local forwards bound to loopback, else None.
    `ports` are the local ports of its `-L` options; user is None when neither `-l` nor `USER@` names one."""
    if not isinstance(doc, dict):
        return None
    args = doc.get("ProgramArguments")
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        return None
    program = doc.get("Program") if isinstance(doc.get("Program"), str) else args[0]
    if Path(program).name != "ssh":
        return None
    ports, user, dest, i = [], None, None, 1
    while i < len(args):
        a = args[i]
        if a == "--":
            dest = args[i + 1] if i + 1 < len(args) else None
            break
        if not a.startswith("-") or a == "-":
            dest = a
            break
        flags, j = a[1:], 0
        while j < len(flags):
            f = flags[j]
            if f in ARG_OPTIONS:
                value = flags[j + 1:] or (args[i + 1] if i + 1 < len(args) else "")
                if not flags[j + 1:]:
                    i += 1
                if f == "L":
                    port = _local_port(value)
                    if port is not None:
                        ports.append(port)
                elif f == "l":
                    user = value
                break
            j += 1
        i += 1
    if dest is None or not ports:
        return None
    if dest.startswith("ssh://"):
        dest = dest[len("ssh://"):].rsplit(":", 1)[0] if dest.count(":") > 1 else dest[len("ssh://"):]
    if "@" in dest:
        user, dest = dest.rsplit("@", 1)
    return {"ports": ports, "user": user, "host": dest}


def _local_port(spec: str) -> int | None:
    """The local port of `-L [bind:]port:host:hostport` when it binds loopback (or no address, which ssh binds
    to loopback by default); a socket path or a wildcard bind is not such a forward."""
    parts = spec.split(":")
    if len(parts) == 4:
        bind, port = parts[0], parts[1]
        if bind not in LOOPBACK:
            return None
    elif len(parts) == 3:
        port = parts[0]
    else:
        return None
    return int(port) if port.isdigit() and 0 < int(port) < 65536 else None


def find_forwards(folder: Path, port: int, skip: str) -> list[tuple[str, Path, dict]]:
    """Every launchd job in FOLDER (but SKIP) whose ssh forwards local PORT: (label, path, parsed)."""
    out = []
    for path in sorted(folder.glob("*.plist")) if folder.is_dir() else []:
        if path.name.startswith(".") or not path.is_file():
            continue
        try:
            doc = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
            continue
        parsed = ssh_forward(doc)
        label = doc.get("Label") if isinstance(doc, dict) and isinstance(doc.get("Label"), str) else path.stem
        if parsed and port in parsed["ports"] and label != skip:
            out.append((label, path, parsed))
    return out


def port_free(port: int, wait: float = PORT_WAIT_SECONDS) -> bool:
    """Whether 127.0.0.1:PORT can be bound here, waiting up to WAIT for a stopped service to let it go.
    SO_REUSEADDR as ssh sets it: a closing connection does not count, a listener does."""
    deadline = time.monotonic() + wait
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
        finally:
            s.close()
        time.sleep(0.25)


def recorded(entry: dict) -> tuple[str | None, bool]:
    """(the forward's label, whether it was adopted) of a placement entry; a malformed field is no forward."""
    label = entry.get("forward")
    if not isinstance(label, str) or not mi.LABEL_SHAPE.fullmatch(label):
        return None, False
    return label, entry.get("forwardAdopted") is True


def _run(runner, argv: list[str]) -> subprocess.CompletedProcess:
    return runner(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def _commands_allowed() -> bool:
    return os.environ.get("OBSERVATORY_SYSTEM_SETUP") != "0"


def loaded(runner, label: str, uid: int) -> bool | None:
    """Whether launchd holds LABEL in this account's GUI domain; None in a sandbox, where nothing is asked."""
    if not _commands_allowed():
        return None
    return _run(runner, ["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0


def _write_private(path: Path, data: bytes) -> None:
    """Atomic and owner-only (0600) in a folder that already exists; the folder's own mode is the person's."""
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _tail(p: subprocess.CompletedProcess) -> str:
    tail = ((p.stderr or "").strip().splitlines() or [""])[-1][:200]
    return f"exit {p.returncode}" + (f": {tail}" if tail else "")


def install(job_label: str, service: dict, user: str, host: str, home: Path, *, runner, uid: int,
            free=port_free) -> dict:
    """Set up the forward of SERVICE's port toward USER@HOST: adopt a matching hand-made job, keep or replace
    the one this engine installed, or install it. Answers `state` (`installed`, `unchanged`, `adopted`,
    `refused`, `failed`) and, for the first three, the `label` the placement file records."""
    port, via = service["port"], f"{user}@{host}"
    own = forward_label(service["serviceId"])
    folder = home / "Library/LaunchAgents"
    out = {"port": port, "via": via}
    others = find_forwards(folder, port, own)
    matching = [o for o in others if o[2]["host"] == host and o[2]["user"] == user]
    if matching:
        label, path, _ = matching[0]
        running = loaded(runner, label, uid)
        out.update({"state": "adopted", "label": label, "adopted": True, "definition": str(path),
                    "running": running})
        if running is False:
            out["hint"] = (f"{label} forwards port {port} to {via} but is not loaded: load it "
                           f"(launchctl bootstrap gui/{uid} {path}) or the port reads as not answering here")
        return out
    if others:
        label, _path, parsed = others[0]
        where = f"{parsed['user'] + '@' if parsed['user'] else ''}{parsed['host']}"
        out.update({"state": "refused", "reason": "forward-elsewhere",
                    "message": f"{label} already forwards 127.0.0.1:{port} to {where}, not to {via}: one port "
                               f"forwards to one place. Move {label} aside (launchctl bootout gui/{uid}/{label}, "
                               "then its plist out of ~/Library/LaunchAgents), or name its destination in --via, "
                               "then run leave again; no forward was installed"})
        return out
    path = plist_path(home, own)
    data = plistlib.dumps(forward_document(own, port, user, host))
    existed = path.is_file()
    same = existed and path.read_bytes() == data
    is_loaded = loaded(runner, own, uid) if existed else False
    if same and is_loaded is not False:
        out.update({"state": "unchanged", "label": own, "adopted": False, "definition": str(path),
                    "running": is_loaded})
        return out
    if not (existed and is_loaded) and not free(port):
        out.update({"state": "refused", "reason": "port-held",
                    "message": f"127.0.0.1:{port} is held here by a process that no forward job defines (not "
                               "one this engine installed, not an ssh -L job in ~/Library/LaunchAgents): the "
                               "service may still be running here, or another program took the port. Find it "
                               f"with `lsof -nP -iTCP:{port} -sTCP:LISTEN`, stop it, then run leave again; no "
                               "forward was installed"})
        return out
    if existed and is_loaded and _commands_allowed():
        p = _run(runner, ["launchctl", "bootout", f"gui/{uid}/{own}"])
        if p.returncode != 0 and loaded(runner, own, uid):
            out.update({"state": "failed", "reason": "unload-failed", "label": own, "adopted": False,
                        "message": f"{own} could not be unloaded to replace it ({_tail(p)}); it stays as it was"})
            return out
    _write_private(path, data)
    out.update({"label": own, "adopted": False, "definition": str(path)})
    if existed and not same:
        out["replaced"] = True
    if not _commands_allowed():
        out.update({"state": "installed", "running": None, "load": "skipped: OBSERVATORY_SYSTEM_SETUP=0"})
        return out
    _run(runner, ["launchctl", "enable", f"gui/{uid}/{own}"])
    p = _run(runner, ["launchctl", "bootstrap", f"gui/{uid}", str(path)])
    if p.returncode != 0:
        path.unlink(missing_ok=True)
        out.pop("definition", None)
        out.update({"state": "failed", "reason": "load-failed", "label": None,
                    "message": f"{own} did not load ({_tail(p)}); its plist was removed, no forward is recorded"})
        return out
    out.update({"state": "installed", "running": True})
    return out


def remove(entry: dict, home: Path, *, runner, uid: int, remove_adopted: bool = False) -> dict:
    """Undo the forward a placement entry records. A job this engine installed (its label has PREFIX and the
    entry does not say adopted) is unloaded and its plist removed; an adopted one is left as it is unless
    REMOVE_ADOPTED, which unloads it and sets its plist aside under the data folder. Answers `state`
    (`removed`, `adopted-left`, `adopted-removed`, `none`) and raises ForwardError when an unload fails."""
    label, adopted = recorded(entry)
    sid = entry.get("serviceId") if isinstance(entry.get("serviceId"), str) else None
    if label is None:
        own = None
        try:
            own = forward_label(sid) if sid else None
        except ForwardError:
            pass
        if own and plist_path(home, own).is_file():
            # Installed here but never recorded (a leave that stopped midway): it is this engine's own job.
            label, adopted = own, False
        else:
            return {"state": "none", "label": None}
    ours = label.startswith(PREFIX) and not adopted
    path = plist_path(home, label)
    if not path.is_file():
        found = [p for p in (home / "Library/LaunchAgents").glob("*.plist")
                 if p.is_file() and mi.job_label(p) == label] if (home / "Library/LaunchAgents").is_dir() else []
        path = found[0] if found else path
    if not ours and not remove_adopted:
        port = None
        try:
            parsed = ssh_forward(plistlib.loads(path.read_bytes())) if path.is_file() else None
            port = parsed["ports"][0] if parsed else None
        except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
            pass
        held = f"port {port}" if port else "its port"
        return {"state": "adopted-left", "label": label, "definition": str(path) if path.is_file() else None,
                "hint": f"{label} was set up by hand and adopted, so it is left running and still holds {held} "
                        "here: the service cannot start here while it does. Run return again with "
                        f"--remove-adopted to unload it and set its plist aside, or launchctl bootout "
                        f"gui/{uid}/{label} yourself"}
    if not ours:
        # Unload and move only what is still an ssh forward of a local port: the record names a label, and the
        # file under it may have changed since it was adopted.
        try:
            still = ssh_forward(plistlib.loads(path.read_bytes())) if path.is_file() else None
        except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
            still = None
        if still is None:
            raise ForwardError(f"{label} is no longer an ssh forward of a local port (its plist changed or is "
                               "gone); nothing was unloaded or moved, and the forward stays recorded")
    if _commands_allowed() and loaded(runner, label, uid):
        p = _run(runner, ["launchctl", "bootout", f"gui/{uid}/{label}"])
        if p.returncode != 0 and loaded(runner, label, uid):
            raise ForwardError(f"{label} is still loaded ({_tail(p)}); the forward stays recorded")
    if ours:
        path.unlink(missing_ok=True)
        return {"state": "removed", "label": label}
    aside = None
    if path.is_file():
        folder = mi.ensure_private_dir(mi.data_dir(home) / "machine-move" / "forwards-set-aside", home)
        target = folder / path.name
        if os.path.lexists(target):
            target = folder / f"{path.stem}.{time.strftime('%Y%m%dT%H%M%S')}{path.suffix}"
        shutil.move(str(path), str(target))
        aside = str(target)
    return {"state": "adopted-removed", "label": label, "set_aside": aside}
