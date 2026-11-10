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

A STAGED COPY CAN BE STALE (OBS-390). A restore stages the other machine's definitions, possibly days
before `cutover`; a service released anew since then lives in a new release folder. Before a staged
launchd definition moves, `cutover` checks that its `Program`, `ProgramArguments[0]` and `WorkingDirectory`
exist here, and refuses that label as `staged-plist-stale`, naming the missing path and the remedy (write
the job's current definition into the staging folder, then cutover again), before anything moves or the
person is asked. A definition already in the job folder is this machine's own and is not judged.

Nothing here reads the other machine: the person runs `leave` there, and `cutover` says so. A load
that fails puts the definition back into the staging folder, so a failed cutover never leaves a job
that would start at the next login. `OBSERVATORY_SYSTEM_SETUP=0` (a test, a sandbox) moves files
and writes the placement file but never calls launchctl or systemctl.

    on either:                      project-observatory full machine-move status [--peer FILE]
        the checklist of the whole move, M1 to M4 (OBS-286, below): one release on both, the
        restore complete, its own SSH key and device id, the vault the same, no job on both
    on the other machine:           project-observatory full machine-move facts --out FILE
        the public facts `status` compares this machine with (see "the checklist of a move")

A PORT FORWARD (OBS-480, machine_forward.py). `leave LABEL --to MACHINE_ID --via USER@HOST` (macOS) also
forwards the local port of the Fabric service LABEL names to the account USER on HOST, through one launchd ssh
job the placement entry names in `forward`; a hand-made forward of that port to that space is adopted.

    on the machine the job comes back to:  project-observatory full machine-move return LABEL [--remove-adopted]
        removes the forward this engine installed (an adopted one only with --remove-adopted) and clears
        `forward`, before `cutover`, which refuses while a forward is recorded

AN ACCOUNT NOBODY LOGS IN TO (OBS-361 part 1). On macOS an account with no graphical login has no
`gui/<uid>` domain, and `launchctl bootstrap user/<uid>` refuses agents even as root, so a job staged in
such a "space" could never start as an agent. When `launchctl print gui/<uid>` answers that the domain does
not exist (exit 112, "could not find domain", or 125, "Domain does not support specified action", which
such an account answers once anything runs as it, an SSH login or its own daemons), `cutover` runs every
check as before, then moves the definition into the background folder (`machine_identity.background_dir`) instead of
~/Library/LaunchAgents and answers `waiting-for-root` with the one command an administrator runs:
`sudo /usr/bin/python3 -I /usr/local/libexec/observatory-space-daemons apply --user ACCOUNT --label LABEL`
(`tools/space_daemons.py`, installed once as a root-owned copy, run in Python's isolated mode). That writes the job to
/Library/LaunchDaemons with `UserName` set to this account: it runs as this account, never as root, and
starts again after a reboot with nobody logged in. Any other answer of `launchctl print gui/<uid>` (0, or a
code this does not know) keeps today's path. `leave` refuses while such a daemon runs (root removes it
first, `... remove --user ACCOUNT --label LABEL`), then works from the background folder as from
~/Library/LaunchAgents. The registry root writes (/Library/Application Support/ProjectObservatory/
space-daemons.json) is only read here.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.parsers.expat

import machine_forward as mf
import machine_identity as mi
import osprivacy

PROG = "project-observatory full"


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


#: `launchctl print gui/<uid>`'s exit codes when the account has no GUI domain to load agents into (measured on
#: macOS 26.6): 112 "could not find domain" while nothing runs as the account, and 125 "Domain does not support
#: specified action" once something does (an SSH login, its own daemons, cfprefsd) but nobody has signed in at the
#: screen; `launchctl bootstrap gui/<uid>` then fails with the same 125. 125 is the steady state of a working space,
#: so every cutover after its first daemon starts answers it. 113 is a missing service, 0 a domain that exists.
NO_DOMAIN = frozenset({112, 125})
#: Where an administrator installs the root-owned copy of tools/space_daemons.py (OBS-361).
SPACE_DAEMONS_COPY = "/usr/local/libexec/observatory-space-daemons"


def gui_session(uid: int, runner) -> bool:
    """Whether this account has a GUI launchd domain to load agents into. Only launchctl's own "no such
    domain" answers say no: any other answer keeps the agent path, which fails and puts back as before."""
    return _run(runner, ["launchctl", "print", f"gui/{uid}"]).returncode not in NO_DOMAIN


def _space_daemons_module():
    tools = str(Path(__file__).resolve().parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import space_daemons
    return space_daemons


def _space_entry(label: str, uid: int) -> dict | None:
    """The registry entry root wrote for LABEL of this account, read only; None when there is none or the
    registry does not read (nothing is known to run then)."""
    try:
        sd = _space_daemons_module()
        entries = sd.read_registry(sd.system_from_environment())
    except Exception:  # an unreadable registry says nothing runs; it is root's file, never repaired here
        return None
    for entry in entries:
        if entry.get("label") == label and entry.get("uid") == uid:
            return entry
    return None


def _system_daemon_loaded(runner, label: str, uid: int) -> bool:
    """LABEL runs here as a system daemon that root's space_daemons.py loaded for this account."""
    if _space_entry(label, uid) is None:
        return False
    return _run(runner, ["launchctl", "print", f"system/{label}"]).returncode == 0


def account_name(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return os.environ.get("USER") or "ACCOUNT"


RELEASES = "https://github.com/passioncode-ai/project-observatory-dashboard/releases/download/vX.Y.Z"
#: Where the organization's release key is published (public), beside its release-signing README.
RELEASE_KEY_URL = ("https://raw.githubusercontent.com/passioncode-ai/.github/main/release-signing/"
                   "passioncode-release-signing.asc")


def release_key_fingerprint() -> str:
    """The pinned release key, from release_signature.PINNED: the one `full update` checks."""
    import release_signature
    return release_signature.PINNED[0]["fingerprint"]


def install_steps() -> list[str]:
    """How an administrator gets the root-owned copy, once per computer (OBS-361): from the release,
    downloaded and verified in their OWN account, never copied from this account's folders (which this
    account could have changed). The signature is checked against the pinned key, never any key gpg holds."""
    fp = release_key_fingerprint()
    return [
        "once per computer, an administrator in their OWN account (never from this account's folders): "
        "gh release download vX.Y.Z -R passioncode-ai/project-observatory-dashboard -p 'SHA256SUMS*' -p '*.whl'  "
        f"(without gh: curl -LO {RELEASES}/SHA256SUMS, the same for SHA256SUMS.asc and "
        "project_observatory-X.Y.Z-py3-none-any.whl)",
        "shasum -a 256 -c SHA256SUMS --ignore-missing",
        f"curl -LO {RELEASE_KEY_URL} && gpg --import passioncode-release-signing.asc  (gpg is not on macOS by "
        "default: brew install gnupg)",
        f"gpg --status-fd 1 --verify SHA256SUMS.asc SHA256SUMS | grep -q \"VALIDSIG {fp}\"  (or, without gpg, "
        "with the administrator's own Observatory install: python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); "
        "import release_signature as r; sys.exit(r.verify(open(\"SHA256SUMS\", \"rb\").read(), "
        f"open(\"SHA256SUMS.asc\").read()) != sys.argv[2])' \"$(project-observatory full-path)\" {fp})",
        "unzip -p project_observatory-X.Y.Z-py3-none-any.whl observatory/engine/tools/space_daemons.py > "
        "space_daemons.py",
        "sudo install -d -o root -g wheel -m 0755 /usr/local/libexec",
        f"sudo install -o root -g wheel -m 0755 space_daemons.py {SPACE_DAEMONS_COPY}",
    ]


def root_step(verb: str, label: str, uid: int) -> str:
    """The administrator's command for LABEL: isolated mode (`-I`), so root imports nothing the account wrote."""
    return f"sudo /usr/bin/python3 -I {SPACE_DAEMONS_COPY} {verb} --user {account_name(uid)} --label {label}"


def _loaded(runner, system: str, label: str, uid: int) -> bool:
    if system == "launchd":
        if _run(runner, ["launchctl", "print", f"gui/{uid}/{label}"]).returncode == 0:
            return True
        return _system_daemon_loaded(runner, label, uid)
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


def _daemon_state(runner, label: str, uid: int, *, ask: bool) -> str:
    """`loaded` (root's daemon of this account runs), `registered` (root wrote it; not loaded, or not asked)
    or `waiting-for-root` (nobody has run space_daemons.py apply for it)."""
    if _space_entry(label, uid) is None:
        return "waiting-for-root"
    if ask and _run(runner, ["launchctl", "print", f"system/{label}"]).returncode == 0:
        return "loaded"
    return "registered"


def show(home: Path, *, platform: str = sys.platform, runner=subprocess.run, uid: int | None = None) -> dict:
    system = _system(platform)
    doc = mi.read_placement(home)
    staging = mi.staging_dir(home, system)
    background = []
    if system == "launchd":
        uid = int(osprivacy.account()) if uid is None else uid
        for p in _definitions(mi.background_dir(home), system):
            label = mi.job_label(p) if p.is_file() else p.stem
            background.append({"label": label, "file": p.name,
                               "daemon": _daemon_state(runner, label, uid, ask=_commands_allowed())})
    return {"machine": mi.machine_id(home), "placement": str(mi.placement_path(home)),
            "elsewhere": doc["elsewhere"], "staging": str(staging),
            "staged": [{"label": mi.job_label(p) if p.is_file() else p.stem, "file": p.name}
                       for p in _definitions(staging, system)],
            "background": background}


def ask_left(label: str, me: str) -> bool:
    """The person's typed word that `leave` ran on the other machine: the label, typed back at a terminal
    the person types in (a real console on Windows, where the NUL device also reads as a tty)."""
    if not osprivacy.is_terminal():
        raise MachineMoveError("cutover asks you to confirm that the job was stopped on the other machine, so it "
                               "runs only at a terminal")
    print(f"Has `project-observatory full machine-move leave {label} --to {me}` run on the other machine?\n"
          "Starting it here while it still runs there runs it twice.", file=sys.stderr)
    try:
        typed = input(f"Type {label} to confirm: ")
    except EOFError:
        return False
    return typed.strip() == label


#: The definition keys whose path must exist before a staged job is moved here (OBS-390).
STALE_KEYS = ("Program", "ProgramArguments", "WorkingDirectory")


def stale_paths(definition: Path) -> list[str]:
    """The paths a staged launchd definition names that do not exist on this machine (OBS-390).

    A restore stages the other machine's definitions; a service released anew since then lives in a new
    release folder, so the staged copy may name a program or a working directory that is gone. Checked:
    `Program`, `ProgramArguments[0]` and `WorkingDirectory`, each only when it is an absolute path (a bare
    program name is launchd's PATH lookup; root's own tool refuses it by its own rule). A definition that
    does not parse is left to the checks that follow, which name it."""
    try:
        doc = plistlib.loads(definition.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
        return []
    if not isinstance(doc, dict):
        return []
    args = doc.get("ProgramArguments")
    # With Program set, launchd runs it and ProgramArguments[0] is only argv[0]: checked when Program is absent.
    first = args[0] if isinstance(args, list) and args and "Program" not in doc else None
    named = [doc.get("Program"), first, doc.get("WorkingDirectory")]
    missing: list[str] = []
    for value in named:
        if isinstance(value, str) and value and os.path.isabs(value) and value not in missing \
                and not os.path.exists(value):
            missing.append(value)
    return missing


def _refuse_stale(label: str, staged: Path, staging: Path) -> None:
    missing = stale_paths(staged)
    if missing:
        raise MachineMoveError(
            f"staged-plist-stale: the staged {staged.name} names a path missing on this machine: "
            f"{', '.join(missing)} (a restore staged it, and its service has since moved, for example to a new "
            f"release folder). Write the job's current definition into {staging} (replacing {staged.name}), then "
            f"run cutover again: `{PROG} machine-move cutover {label}`; nothing moved")


def cutover(label: str, home: Path, *, runner=subprocess.run, platform: str = sys.platform,
            uid: int | None = None, confirm=None) -> dict:
    """Move one staged job here: into the job folder, enabled and loaded, and out of `elsewhere`.
    `confirm(label, this machine's id)` answers whether the person confirmed that `leave` ran on the
    other machine (`ask_left` at a terminal); without it, nothing moves."""
    if not mi.LABEL_SHAPE.fullmatch(label or ""):
        raise MachineMoveError("a label is letters, digits or . _ @ : -")
    system = _system(platform)
    uid = int(osprivacy.account()) if uid is None else uid
    staging, folder = mi.staging_dir(home, system), job_folder(home, system)
    staged = _find(staging, system, label)
    live = _find(folder, system, label)
    me = mi.machine_id(home)
    # OBS-361: an account with no GUI session cannot load an agent; its jobs run as system daemons.
    headless = system == "launchd" and _commands_allowed() and not gui_session(uid, runner)
    background = _find(mi.background_dir(home), system, label) if headless else None
    if staged is None and live is None and background is None:
        raise MachineMoveError(f"no job {label} is staged here ({staging}) or defined here ({folder}); "
                               "`full machine-move show` lists the staged ones")
    if staged is not None and system == "launchd":
        # OBS-390: a staged copy can be days old; one naming a path gone from here never moves.
        _refuse_stale(label, staged, staging)
    held = next((mf.recorded(e)[0] for e in mi.read_placement(home)["elsewhere"] if e["label"] == label), None)
    if held is not None:
        # OBS-480: the forward of its port still holds the port here, so the service could not take it.
        raise MachineMoveError(f"a port forward ({held}) of {label} is recorded here and holds its port: run "
                               f"`{PROG} machine-move return {label}` first; nothing moved")
    if headless:
        default = home / ".local" / "share" / mi.FOLDER
        if mi.data_dir(home) != default:
            raise MachineMoveError(
                f"this account has no GUI session, so {label} can run here only as a system daemon, and the "
                f"administrator's tool reads {default}, not XDG_DATA_HOME ({mi.data_dir(home)}): a custom "
                "XDG_DATA_HOME is not supported for such an account yet (OBS-361); nothing moved")
        if live is not None and (staged is not None or background is not None):
            other = staged if staged is not None else background
            raise MachineMoveError(f"{label} is defined twice here: {live} and {other}; one job runs once, so move "
                                   "one of them aside first; nothing moved")
        file_label = _check_as_root_will(staged or live or background, home, uid)
    if not (confirm or ask_left)(label, me):
        raise MachineMoveError(f"not confirmed: nothing moved. Run `full machine-move leave {label} --to {me}` on "
                               "the other machine first")
    if headless:
        return _cutover_to_background(label, home, runner, uid, me, staged or live, background, file_label)
    moved_here = False
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
            moved_here = True
        live = target
    loaded = "skipped: OBSERVATORY_SYSTEM_SETUP=0"
    if _commands_allowed():
        try:
            _start(runner, system, label, live, uid)
            loaded = "loaded"
        except (MachineMoveError, OSError, subprocess.SubprocessError) as exc:
            if not moved_here:
                # This machine's own definition (it was not staged): it stays where it was.
                raise MachineMoveFailed(f"{exc}; {live} was already this machine's and stays where it was") from None
            mi.ensure_private_dir(staging, home)
            shutil.move(str(live), str(staging / live.name))
            raise MachineMoveFailed(f"{exc}; its definition is back in {staging}, so nothing starts it at login")
    was_elsewhere = mi.remove_elsewhere(label, home, machine=me)
    return {"label": label, "machine": me, "definition": str(live), "load": loaded,
            "was_elsewhere": was_elsewhere, "placement": str(mi.placement_path(home)),
            "next": [f"on the machine it left, if not done yet: project-observatory full machine-move leave {label} "
                     f"--to {me}  (stops it there, so it never runs on both)"]}


def _space_account(home: Path, uid: int):
    sd = _space_daemons_module()
    name, gid, group = account_name(uid), os.getgid() if hasattr(os, "getgid") else 0, "staff"
    try:
        import grp
        import pwd
        entry = pwd.getpwuid(uid)
        gid = entry.pw_gid
        group = grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        pass
    return sd.Account(name, uid, gid, group, str(home))


def _check_as_root_will(path: Path, home: Path, uid: int) -> str:
    """The administrator's `apply` rules on this definition, before anything moves: the same parse
    (space_daemons.parse_plist, XML comments holding `--` included), the trust file read only, and
    space_daemons.validate. A job root would refuse never waits for root. Its folder rules are root's alone
    and checked there. Answers the definition's Label, which names the moved file."""
    sd = _space_daemons_module()
    if path.is_symlink() or not path.is_file():
        raise MachineMoveError(f"{path} is a link or not a file: root reads only a regular file; nothing moved")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MachineMoveError(f"{path} cannot be read ({type(exc).__name__}); nothing moved") from None
    doc, code, detail = sd.parse_plist_detail(data)
    if code is not None:
        raise MachineMoveError(f"the administrator's tool would refuse {path}: {code}"
                               + (f" ({detail})" if detail else "") + ". It stays where it was")
    label = doc.get("Label")
    name = f"{label}.plist" if isinstance(label, str) else path.name
    account = _space_account(home, uid)
    trust = sd.read_trust(sd.system_from_environment(), account)
    problems, _daemon = sd.validate(doc, name, account, None, trust)
    if problems:
        named = ", ".join(p["code"] + (f" ({p['detail']})" if p["detail"] else "") for p in problems)
        raise MachineMoveError(f"the administrator's tool would refuse {path}: {named}. It stays where it was; "
                               "fix the definition and run cutover again")
    return label


def _cutover_to_background(label: str, home: Path, runner, uid: int, me: str, source: Path | None,
                           background: Path | None, file_label: str | None = None) -> dict:
    """`cutover` for an account with no GUI session (OBS-361): the definition goes to the background
    folder, writable by this account alone, where the administrator's space_daemons.py reads it. Nothing
    is put back on the way: the job is placed here, and runs once root loads it."""
    folder = mi.background_dir(home)
    if source is None:
        target = background
    else:
        mi.ensure_private_dir(folder, home)
        # Named <Label>.plist, the only name root's tool accepts for it.
        target = folder / f"{file_label or mi.job_label(source)}.plist"
        if os.path.lexists(target):
            if target.is_file() and source.is_file() and target.read_bytes() == source.read_bytes():
                source.unlink()
            else:
                raise MachineMoveError(f"{target} already exists and differs from {source}; move one of them aside "
                                       "first")
        else:
            shutil.move(str(source), str(target))
    if target.is_file() and not target.is_symlink():
        # Root reads only a definition nobody but this account can change (space_daemons.py).
        target.chmod(target.stat().st_mode & 0o7755 & ~0o022)
    running = _system_daemon_loaded(runner, label, uid)
    step = root_step("apply", label, uid)
    was_elsewhere = mi.remove_elsewhere(label, home, machine=me)
    leave_step = (f"on the machine it left, if not done yet: project-observatory full machine-move leave {label} "
                  f"--to {me}  (stops it there, so it never runs on both)")
    result = {"label": label, "machine": me, "definition": str(target), "domain": "system",
              "load": "loaded" if running else "waiting-for-root",
              "state": "complete" if running else "waiting-for-root",
              "was_elsewhere": was_elsewhere, "placement": str(mi.placement_path(home))}
    if running:
        result["next"] = [leave_step]
    else:
        result["human_steps"] = install_steps() + [
            f"then, once per job: {step}  (this account has no GUI session, so the job runs as a system daemon "
            "with UserName set to this account, never as root, and starts again after a reboot)"]
        result["next"] = [step, leave_step]
    return result


def leave(label: str, to: str, home: Path, *, runner=subprocess.run, platform: str = sys.platform,
          uid: int | None = None, via: str | None = None, port_free=None) -> dict:
    """Stop one job here, stage its definition and name it as running on machine `to`. With `via`
    (USER@HOST, OBS-480) also forward the service's local port to that space (machine_forward.py)."""
    if not mi.LABEL_SHAPE.fullmatch(label or ""):
        raise MachineMoveError("a label is letters, digits or . _ @ : -")
    if not mi.valid_id(to):
        raise MachineMoveError("--to takes the other machine's id (`full machine-move show` prints it there)")
    system = _system(platform)
    uid = int(osprivacy.account()) if uid is None else uid
    me = mi.machine_id(home)
    if to == me:
        raise MachineMoveError("--to names this machine; run `leave` on the machine the job leaves")
    service = mf.service_of(home, label)
    user = host = None
    if via is not None:
        # Every check of the forward runs before anything moves: a refused --via leaves the job as it was.
        if system != "launchd":
            raise MachineMoveError(f"--via: {mf.macos_only()}; nothing moved")
        try:
            user, host = mf.parse_via(via)
        except mf.ForwardError as exc:
            raise MachineMoveError(f"{exc}; nothing moved") from None
        if service is None:
            raise MachineMoveError(f"--via: no Fabric service descriptor names the launchd label {label}, so there "
                                   "is no port to forward; leave without --via moves the job alone; nothing moved")
        if service["port"] is None:
            raise MachineMoveError(f"--via: the service {service['serviceId']} has no local origin "
                                   "(http://127.0.0.1:PORT, 1024 to 65535), so there is no port here to forward; "
                                   "nothing moved")
    staging, folder = mi.staging_dir(home, system), job_folder(home, system)
    if system == "launchd" and _space_entry(label, uid) is not None:
        # Root registered it as a system daemon of this account (OBS-361), loaded or not: only root's `remove`
        # takes it out of launchd's reach, sandbox or not.
        raise MachineMoveError(f"{label} is registered by root as a system daemon of this account, which only root "
                               f"removes: {root_step('remove', label, uid)}, then run leave again; nothing moved")
    live = _find(folder, system, label)
    if live is None and system == "launchd":
        live = _find(mi.background_dir(home), system, label)
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
    entry = {"label": label, "serviceId": _services(home).get(label), "placedOn": to, "by": "machine-move leave"}
    if via is not None:
        try:
            forward = mf.install(label, service, user, host, home, runner=runner, uid=uid,
                                 free=port_free or mf.port_free)
        except (OSError, subprocess.SubprocessError, mf.ForwardError) as exc:
            # The job has moved: the placement entry is written whatever happens to the forward.
            forward = {"state": "failed", "reason": "error", "port": service["port"], "via": via,
                       "message": f"the forward could not be set up ({type(exc).__name__}); run leave again "
                                  "with --via to retry"}
        if forward["state"] in ("installed", "unchanged", "adopted"):
            entry.update({"forward": forward["label"], "forwardAdopted": forward["adopted"]})
        elif not _recorded_forward_present(label, home):
            entry["forward"] = None
        # else: a refused or failed forward leaves a recorded forward that still has its plist here as it was
        # (an unload that failed keeps the old job loaded; a rerun to another space leaves an adopted one
        # holding the port), so `cutover` still refuses and `return` still finds it.
    else:
        forward = _forward_without_via(label, home, system, service)
    path = mi.place_elsewhere([entry], home, machine=me)
    return {"label": label, "machine": me, "placed_on": to, "stop": stopped, "staging": str(staging),
            "placement": str(path), "forward": forward,
            "next": [f"on {to}: project-observatory full machine-move cutover {label}"]}


def _recorded_forward_present(label: str, home: Path) -> bool:
    """Whether the placement entry of LABEL records a forward whose plist is still in this account's
    LaunchAgents (OBS-480): such a forward still holds the port, so a refused or failed rerun keeps its record."""
    entry = next((e for e in mi.read_placement(home)["elsewhere"] if e["label"] == label), None)
    recorded, _ = mf.recorded(entry or {})
    if recorded is None:
        return False
    if mf.plist_path(home, recorded).is_file():
        return True
    # A hand-made forward that was adopted may sit in a file not named after its label.
    agents = home / "Library/LaunchAgents"
    return agents.is_dir() and any(p.is_file() and mi.job_label(p) == recorded for p in agents.glob("*.plist"))


def _forward_without_via(label: str, home: Path, system: str, service: dict | None) -> dict:
    """What `leave` without --via says of the forward (OBS-480): the one already recorded is kept; else the port
    a forward would carry and how to ask for it, or why there is none."""
    try:
        doc = mi.read_placement(home)
    except mi.MachineIdentityError:
        doc = {"elsewhere": []}
    kept = next((mf.recorded(e) for e in doc["elsewhere"] if e["label"] == label), (None, False))
    if kept[0] is not None:
        return {"state": "kept", "label": kept[0], "adopted": kept[1]}
    if service is None or service["port"] is None:
        return {"state": "not-applicable",
                "reason": "no Fabric service descriptor with a local origin names this label"}
    if system != "launchd":
        return {"state": "not-applicable", "port": service["port"], "reason": mf.macos_only()}
    return {"state": "not-requested", "port": service["port"],
            "hint": f"{service['serviceId']} answers at 127.0.0.1:{service['port']} here: run leave again with "
                    "--via USER@HOST (the account and machine it now runs on) to forward that port there"}


def return_job(label: str, home: Path, *, runner=subprocess.run, platform: str = sys.platform,
               uid: int | None = None, remove_adopted: bool = False) -> dict:
    """The move back of a job `leave` placed elsewhere (OBS-480), its first step on this machine: remove the
    forward of its port, so the service can take the port again at `cutover`. The job stays placed on the other
    machine until `leave` runs there and `cutover` here."""
    if not mi.LABEL_SHAPE.fullmatch(label or ""):
        raise MachineMoveError("a label is letters, digits or . _ @ : -")
    if _system(platform) != "launchd":
        raise MachineMoveError(f"{mf.macos_only()}; nothing to remove on this system")
    uid = int(osprivacy.account()) if uid is None else uid
    me = mi.machine_id(home)
    entry = next((e for e in mi.read_placement(home)["elsewhere"] if e["label"] == label), None)
    if entry is None:
        raise MachineMoveError(f"{label} is not placed elsewhere here ({mi.placement_path(home)}); nothing to "
                               "return. `full machine-move show` lists what is")
    try:
        forward = mf.remove(entry, home, runner=runner, uid=uid, remove_adopted=remove_adopted)
    except mf.ForwardError as exc:
        raise MachineMoveFailed(str(exc)) from None
    mi.set_forward(label, home, forward=None, machine=me)
    there = entry.get("placedOn") if mi.valid_id(entry.get("placedOn")) else "the machine it runs on"
    return {"label": label, "machine": me, "placed_on": entry.get("placedOn"), "forward": forward,
            "placement": str(mi.placement_path(home)),
            "next": [f"on {there}: project-observatory full machine-move leave {label} --to {me}",
                     f"here: project-observatory full machine-move cutover {label}"]}


# --- the checklist of a move (OBS-286) ----------------------------------------------------------
#
# `full machine-move status` answers, on one machine, whether the move between two machines is done:
#
#   M1 release    both machines run the same engine release
#   M2 restore    the machine restored here completely (or the other one did, from this one's bundle)
#   M3 ssh-key    this machine signs with an SSH key of its own, never a copy of the other machine's
#   M3 device-id  this machine has its own device id in the agent network (`full node --init`)
#   M3 vault      every vault slot reads `same` on both machines (`full vault-digests`, OBS-194)
#   M4 jobs       no background job is defined on both machines
#
# Each item is `ok`, `missing` (with the step that fixes it) or `unknown`, and names its evidence.
# Nothing here reaches the other machine: what it knows of it comes from that machine's FACTS, a
# small owner-only file it writes with `full machine-move facts --out FILE` (its id, release, the
# fingerprints of its SSH keys, its device id, the jobs defined there, its restore receipt), carried
# here by the person (the backups root both machines read works) and named with `--peer FILE` or put
# in `<data folder>/machine-move/peers/`. Facts older than `--max-age-hours` (48) say nothing about
# today's release or jobs, so those items read `unknown`, never a guess; key fingerprints and device
# ids do not change by themselves, so an older file still tells them apart. Facts and status carry
# public facts only: fingerprints, ids, labels, versions and counts, never a key or a value. A facts file
# holds only what `read_facts` takes (a key name it would refuse is masked, a label it would refuse is left
# out, `omitted` counts both), and a file of any other shape is refused by name in `degraded`, never a crash.
# Status reads and changes nothing: no file is written (not even this machine's id, which a machine without
# a platform identifier would otherwise keep), and no job is started, stopped or listed by launchctl.

FACTS_SCHEMA = "observatory-machine-facts/1"
STATUS_SCHEMA = "observatory-machine-move-status/1"
MAX_AGE_HOURS = 48
#: How far ahead of this machine's clock a peer's facts may be dated before they read as from the future.
CLOCK_SKEW = datetime.timedelta(minutes=5)
FACTS_MAX_BYTES = 1 << 20
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
#: ssh's default identity files: offered to every host whose config names no IdentityFile (tools/ssh_keys.py).
DEFAULT_IDENTITIES = ("id_ed25519", "id_ecdsa", "id_ecdsa_sk", "id_ed25519_sk", "id_rsa", "id_dsa")
#: A key `ssh-keys rekey OLD` takes (tools/ssh_keys.py KEY_REF): in ~/.ssh or one folder below, never a `.pub`.
KEY_REF = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,63}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VERSION_SHAPE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
DEVICE_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}")
SHOWN_FILE = re.compile(r"~/[A-Za-z0-9._/ -]{1,200}")
OK, MISSING, UNKNOWN = "ok", "missing", "unknown"
ITEMS = (("release", "M1", "the same release on both machines"),
         ("restore", "M2", "the restore complete"),
         ("ssh-key", "M3", "its own SSH key"),
         ("device-id", "M3", "its own device id"),
         ("vault", "M3", "every vault slot reads same"),
         ("jobs", "M4", "no job or bot defined on both machines"))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _iso(at: datetime.datetime) -> str:
    return at.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(text) -> datetime.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value if 2000 <= value.year <= 2999 else None


def peers_dir(home: Path) -> Path:
    return mi.data_dir(home) / "machine-move" / "peers"


def _ssh_keys_module():
    tools = str(Path(__file__).resolve().parent / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import ssh_keys
    return ssh_keys


def own_keys(home: Path) -> tuple[list[dict], list[str]]:
    """The private SSH keys in `~/.ssh` and one folder below, each `{file, fingerprint}`; the fingerprint
    comes from the key's clear header or its `.pub`, so the private section is never read."""
    folder, out, notes = home / ".ssh", [], []
    try:
        sk = _ssh_keys_module()
    except ImportError as exc:
        return [], [f"the SSH key reader did not load ({type(exc).__name__})"]
    if not folder.is_dir():
        return [], []
    files, _pubs, _links = sk._candidates(folder)
    for path in files:
        try:
            header = sk.private_header(path)
        except (OSError, ValueError) as exc:
            notes.append(f"{mb_display(path, home)} could not be read ({type(exc).__name__})")
            continue
        if header is None:
            continue
        fp = header.get("fingerprint")
        if not fp:
            try:
                fp = sk.read_public(Path(str(path) + ".pub"))["fingerprint"]
            except (sk.Refused, OSError, ValueError, KeyError):
                fp = None
        out.append({"file": mb_display(path, home), "fingerprint": fp})
    return sorted(out, key=lambda k: k["file"]), notes


def mb_display(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def device_id(base: Path | None) -> tuple[str | None, str | None]:
    """(this machine's device id in the agent network, why there is none): read from the public
    `device.json` beside the device keys, never from a key."""
    override = os.environ.get("OBSERVATORY_NODE")
    if base is None and not override:
        return None, "no Observatory workspace here, so no device keys"
    folder = (Path(override).expanduser() if override else base / "node") / "device"
    try:
        doc = json.loads(osprivacy.read_regular(folder / "device.json", 65536).decode("utf-8"))
    except FileNotFoundError:
        return None, "no device keys here yet"
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, f"device.json could not be read ({type(exc).__name__})"
    value = doc.get("id") if isinstance(doc, dict) else None
    if not isinstance(value, str) or not DEVICE_SHAPE.fullmatch(value):
        return None, "device.json names no device id"
    return value, None


def defined_jobs(home: Path, platform: str = sys.platform) -> tuple[list[str] | None, str | None]:
    """(the labels of the jobs defined in this machine's job folder, which load at the next login, and in
    its background folder, which root runs as system daemons of an account with no GUI session (OBS-361),
    or None, why not). This engine's own jobs are left out: each machine installs its own."""
    try:
        system = _system(platform)
    except MachineMoveError as exc:
        return None, str(exc)
    import machine_backup
    labels = []
    folders = [job_folder(home, system)]
    if system == "launchd":
        # A job root runs as a system daemon of this account (OBS-361) is defined here too.
        folders.append(mi.background_dir(home))
    for path in [p for f in folders for p in _definitions(f, system)]:
        if path.name.startswith(machine_backup.OWN_JOB_PREFIXES):
            continue
        if path.is_file() and path.suffix == ".plist":
            try:
                doc = plistlib.loads(path.read_bytes())
                if isinstance(doc, dict) and doc.get("Disabled") is True:
                    continue
            except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError):
                pass
        label = mi.job_label(path) if path.is_file() else path.stem
        if label.startswith(machine_backup.OWN_JOB_PREFIXES):
            continue
        labels.append(label)
    return sorted(set(labels)), None


def _space_evidence(home: Path, platform: str) -> list[str]:
    """One line per job in the background folder (OBS-361): registered by root as a system daemon of this
    account, or waiting for the administrator's command. Read from root's registry; launchctl is not asked."""
    if platform != "darwin":
        return []
    uid = int(osprivacy.account())
    lines = []
    for path in _definitions(mi.background_dir(home), "launchd"):
        label = mi.job_label(path) if path.is_file() else path.stem
        if _space_entry(label, uid) is not None:
            lines.append(f"{label} runs as a system daemon of this account (registered by root)")
        else:
            lines.append(f"{label} waits for root: {root_step('apply', label, uid)}")
    return lines


def _receipt(home: Path) -> tuple[dict | None, str | None]:
    try:
        return mi.read_restore_receipt(home), None
    except mi.MachineIdentityError as exc:
        return None, str(exc)


def restore_summary(receipt: dict | None) -> dict | None:
    """What a restore receipt says, shortened: where the bundle came from and whether it is complete."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("apply"), dict):
        return None
    applied = receipt["apply"]
    counts = applied.get("counts") if isinstance(applied.get("counts"), dict) else {}

    def number(value) -> int:
        # The receipt is this engine's, but a file a person edited may hold anything: a count that is not a
        # whole number reads as one problem, never as a crash.
        return value if type(value) is int and value >= 0 else (0 if value in (None, 0, False) else 1)
    gaps = []
    if number(applied.get("failed")):
        gaps.append(f"{number(applied['failed'])} item(s) failed to write")
    if number(counts.get("kept")):
        gaps.append(f"{number(counts['kept'])} conflict(s) kept, not restored")
    if applied.get("placement_error") is not False and applied.get("placement_error") is not None:
        gaps.append("the staged jobs were not named in the placement file")
    for part in ("packages", "clone"):
        done = receipt.get(part)
        if isinstance(done, dict) and number(done.get("failed")):
            gaps.append(f"{number(done['failed'])} {'checkout(s)' if part == 'clone' else 'inventory step(s)'} failed")
    left = applied.get("left_out")
    return {"taken_on": applied.get("taken_on") if mi.valid_id(applied.get("taken_on")) else None,
            "mode": applied.get("mode")[:32] if isinstance(applied.get("mode"), str) else "",
            "at": applied.get("at")[:32] if isinstance(applied.get("at"), str) else "",
            "complete": not gaps, "gaps": gaps,
            "left_out": [c[:32] for c in left if isinstance(c, str)][:20] if isinstance(left, list) else [],
            "cloned": isinstance(receipt.get("clone"), dict)}


def facts(home: Path, base: Path | None, *, platform: str = sys.platform, at: datetime.datetime | None = None) -> dict:
    """This machine's public facts for the other machine's `machine-move status`."""
    keys, _ = own_keys(home)
    device, _ = device_id(base)
    jobs, _ = defined_jobs(home, platform)
    receipt, _ = _receipt(home)
    try:
        elsewhere = sorted(e["label"] for e in mi.read_placement(home)["elsewhere"])
    except mi.MachineIdentityError:
        elsewhere = []
    import configuration as config
    # What is written here is held to the rules `read_facts` reads it by, so the other machine never refuses
    # the whole file for one entry: a key whose file name the reader would refuse keeps its fingerprint under
    # a masked name, and a label it would refuse is left out; `omitted` says how many of each.
    ssh, masked = [], 0
    for k in keys:
        if not k["fingerprint"] or not FINGERPRINT.fullmatch(k["fingerprint"]):
            continue
        name = k["file"]
        if not SHOWN_FILE.fullmatch(name):
            masked += 1
            name = f"~/.ssh/name-not-shown-{masked}"
        ssh.append({"file": name, "fingerprint": k["fingerprint"]})
    shaped = {}
    omitted = {"ssh_names_masked": masked}
    for field, values in (("jobs", jobs), ("elsewhere", elsewhere)):
        kept = None if values is None else [v for v in values if mi.LABEL_SHAPE.fullmatch(v)]
        shaped[field] = kept
        omitted[f"{field}_left_out"] = 0 if values is None else len(values) - len(kept)
    if device is not None and not DEVICE_SHAPE.fullmatch(device):
        device = None
    summary = restore_summary(receipt)
    if summary is not None:
        summary["gaps"] = [g[:200] for g in summary["gaps"]][:20]
    return {"schema": FACTS_SCHEMA, "machine": mi.machine_id(home), "generated_at": _iso(at or _now()),
            "engine_version": config.VERSION, "platform": platform, "ssh": ssh, "device": device,
            "jobs": shaped["jobs"], "elsewhere": shaped["elsewhere"], "restore": summary, "omitted": omitted}


def read_facts(path: Path) -> dict:
    """One peer's facts, validated; a refusal names what is wrong, never the file's content."""
    try:
        raw = osprivacy.read_regular(path, FACTS_MAX_BYTES)
        doc = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        raise MachineMoveError("no such file") from None
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise MachineMoveError("not a readable facts file (a regular JSON file of at most 1 MB)") from None
    if not isinstance(doc, dict) or doc.get("schema") != FACTS_SCHEMA:
        raise MachineMoveError(f"not an {FACTS_SCHEMA} file (`full machine-move facts --out FILE` writes one)")
    if not mi.valid_id(doc.get("machine")):
        raise MachineMoveError("its machine id is malformed")
    if _parse_time(doc.get("generated_at")) is None:
        raise MachineMoveError("its generated_at is not a time")
    version = doc.get("engine_version")
    if version is not None and not (isinstance(version, str) and VERSION_SHAPE.fullmatch(version)):
        raise MachineMoveError("its engine_version is malformed")
    ssh = doc.get("ssh")
    ssh = [] if ssh is None else ssh
    if not isinstance(ssh, list) or len(ssh) > 256 or not all(
            isinstance(k, dict) and isinstance(k.get("fingerprint"), str) and FINGERPRINT.fullmatch(k["fingerprint"])
            and isinstance(k.get("file"), str) and SHOWN_FILE.fullmatch(k["file"]) for k in ssh):
        raise MachineMoveError("its ssh list is malformed")
    device = doc.get("device")
    if device is not None and not (isinstance(device, str) and DEVICE_SHAPE.fullmatch(device)):
        raise MachineMoveError("its device id is malformed")
    for field in ("jobs", "elsewhere"):
        value = doc.get(field)
        if value is not None and (not isinstance(value, list) or len(value) > 4096
                                  or not all(isinstance(v, str) and mi.LABEL_SHAPE.fullmatch(v) for v in value)):
            raise MachineMoveError(f"its {field} list is malformed")
    restore = doc.get("restore")
    if restore is not None:
        # Every member the reader uses, by type and size: a facts file is carried by hand from another machine.
        if not isinstance(restore, dict) or not isinstance(restore.get("complete"), bool):
            raise MachineMoveError("its restore summary is malformed: complete is not true or false")
        if restore.get("taken_on") is not None and not mi.valid_id(restore.get("taken_on")):
            raise MachineMoveError("its restore summary is malformed: taken_on is not a machine id")
        if restore.get("at") is not None and not (isinstance(restore["at"], str) and len(restore["at"]) <= 64):
            raise MachineMoveError("its restore summary is malformed: at is not a time")
        gaps = restore.get("gaps")
        if gaps is not None and (not isinstance(gaps, list) or len(gaps) > 64
                                 or not all(isinstance(g, str) and len(g) <= 400 for g in gaps)):
            raise MachineMoveError("its restore summary is malformed: gaps is not a list of short texts")
    return {"machine": doc["machine"], "generated_at": doc["generated_at"], "engine_version": version,
            "ssh": [{"file": k["file"], "fingerprint": k["fingerprint"]} for k in ssh], "device": device,
            "jobs": doc.get("jobs"), "elsewhere": doc.get("elsewhere") or [],
            "restore": ({"taken_on": restore.get("taken_on"), "complete": restore["complete"],
                         "at": str(restore.get("at") or "")[:32],
                         "gaps": [g[:200] for g in restore.get("gaps") or []][:20]} if restore is not None else None)}


def load_peers(home: Path, files: list[Path] | None = None) -> tuple[list[dict], list[dict]]:
    """(valid peer facts, `{file, reason}` for each file refused): the named files, else every `*.json`
    in the peers folder."""
    if files is None:
        folder = peers_dir(home)
        files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    good, bad = [], []
    for path in files:
        name = path.name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}", path.name) else "a file (name not shown)"
        try:
            good.append(read_facts(path))
        except MachineMoveError as exc:
            bad.append({"file": name, "reason": str(exc)})
        except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
            # A shape the checks above did not foresee: the file is refused and named, status still runs.
            bad.append({"file": name, "reason": f"its content is malformed ({type(exc).__name__})"})
    return good, bad


def _vault_view(max_age_hours: float) -> tuple[dict | None, str | None]:
    """The vault-digests view (OBS-194) computed here without writing anything: this machine's
    digests in memory under the estate key it already holds, against the peer manifests. Never
    creates the key, never prints a digest."""
    try:
        tools = str(Path(__file__).resolve().parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import vault_digests as vd
    except Exception as exc:  # noqa: BLE001: a workspace without a vault is a reason, not a crash
        return None, f"the vault could not be read here ({type(exc).__name__})"
    try:
        key = vd._key(create=False)
        if not key:
            return None, ("this machine holds no estate digest key: `project-observatory full vault-digests key "
                          "import FILE` with the other machine's `key export`")
        local = vd.build_manifest(vd.machine_name(), key)
        peers, bad = vd.read_peers(vd.peers_dir())
        return vd.compare(local, peers, max_age_hours=max_age_hours, unreadable=bad), None
    except Exception as exc:  # noqa: BLE001: Refused, a vault boundary, an OS error: all name a reason
        reason = str(exc) if exc.__class__.__name__ == "Refused" else type(exc).__name__
        return None, f"the vault digests could not be compared: {reason}"


def _rekey_steps(old: str, fingerprint: str | None, new: str | None = None) -> list[str]:
    """`ssh-keys rekey` as the second-machine restore spells it: the plan, then the same with --apply."""
    step = f"project-observatory full ssh-keys rekey {shlex.quote(old)}"
    if new:
        step += f" --name {shlex.quote(new)}"
    if fingerprint:
        step += f" --fingerprint {shlex.quote(fingerprint)}"
    return [step, step + " --apply"]


def _ssh_names(folder: Path) -> set[str]:
    """Every name in ~/.ssh, a `.pub` counted under its key's name. Raises OSError when it cannot be listed."""
    return {p.name[:-len(".pub")] if p.name.endswith(".pub") else p.name for p in folder.iterdir()}


def _own_pair(home: Path, name: str) -> str | None:
    """The fingerprint of a key pair `~/.ssh/NAME` and `~/.ssh/NAME.pub` here (a private file and a
    public line that reads), else None. Only the public half is read."""
    folder = home / ".ssh"
    try:
        sk = _ssh_keys_module()
    except ImportError:
        return None
    try:
        if not (folder / name).is_file() or (folder / name).is_symlink():
            return None
        fp = sk.read_public(folder / f"{name}.pub")["fingerprint"]
    except (sk.Refused, OSError, ValueError, KeyError, TypeError):   # a missing or unreadable half: no pair
        return None
    return fp if isinstance(fp, str) and FINGERPRINT.fullmatch(fp) else None


def _new_key_name(old: str, taken: set[str], handed: set[str], mine=lambda name: None) -> tuple[str, str | None]:
    """(the name for this machine's own key beside a copy of the other machine's, the fingerprint when
    that name is already this machine's key pair). Directly in ~/.ssh (tools/ssh_keys.py NAME): `keys/id`
    becomes `keys-id-own`. A name already holding this machine's own pair (`mine` gives its fingerprint,
    never one of the other machine's) is that key, made by an earlier `rekey --apply`: it is reused, so a
    re-run of the checklist hands out the same steps (rekey reuses a pair and sends only what is missing).
    A name taken by anything else, or already handed out by this checklist, gets a number."""
    stem = old.replace("/", "-")[:56]
    new, n = f"{stem}-own", 2
    while True:
        if new not in handed:
            if new not in taken:
                handed.add(new)
                return new, None
            made = mine(new)
            if made:
                handed.add(new)
                return new, made
        new, n = f"{stem}-own{n}", n + 1


def _other_key_names(withheld, others: list[dict]) -> list[tuple[str, str]]:
    """(name, fingerprint) of each key the other machine holds, as rekey takes it: first the keys the
    restore receipt lists as withheld (`other_keys`), then the keys in the peers' facts directly in
    ~/.ssh. A name rekey would refuse is left out, never repaired."""
    out: dict[str, str] = {}
    for k in withheld if isinstance(withheld, list) else []:
        # Only a key the restore saw directly in ~/.ssh (`in_ssh`): the restore gives any other a note
        # to make by hand and no step, and a receipt older than the field does not say where it sat.
        if not isinstance(k, dict) or k.get("in_ssh") is not True:
            continue
        name, fp = k.get("key"), k.get("fingerprint")
        if isinstance(name, str) and isinstance(fp, str) and FINGERPRINT.fullmatch(fp) \
                and KEY_REF.fullmatch(name) and "/" not in name and not name.endswith(".pub"):
            out.setdefault(name, fp)
    for peer in others:
        for k in peer.get("ssh") or []:
            name = k["file"][len("~/.ssh/"):] if k["file"].startswith("~/.ssh/") else ""
            if KEY_REF.fullmatch(name) and "/" not in name and not name.startswith("name-not-shown-"):
                out.setdefault(name, k["fingerprint"])
    return sorted(out.items())


def _item(key: str, state: str, evidence: list[str], step: list[str] | None = None) -> dict:
    step_id, title = next((s, t) for k, s, t in ITEMS if k == key)
    out = {"id": key, "step": step_id, "title": title, "state": state, "evidence": evidence}
    if step:
        out["next"] = step
    return out


def status(home: Path, base: Path | None, *, peers: list[dict] | None = None, unreadable: list[dict] | None = None,
           platform: str = sys.platform, at: datetime.datetime | None = None, max_age_hours: float = MAX_AGE_HOURS,
           vault_view=None) -> dict:
    """The checklist M1 to M4 on this machine, from what it holds and the peers' facts. Changes nothing."""
    import configuration as config
    at = at or _now()
    # Read-only: a machine with no platform identifier and no kept id gets none here (any restore,
    # `machine-move show` or `facts` makes it), and the checklist says so.
    me = mi.machine_id(home, create=False)
    degraded = [{"source": "peers", "code": "unreadable-facts", "reason": f"{b['file']}: {b['reason']}"}
                for b in unreadable or []]
    if me is None:
        degraded.append({"source": "machine", "code": "no-machine-id",
                         "reason": "this machine has no id yet (no platform identifier and none kept); "
                                   "`project-observatory full machine-move show` makes one"})
    newest: dict[str, dict] = {}
    for peer in peers or []:
        if peer["machine"] == me:
            degraded.append({"source": "peers", "code": "this-machine",
                             "reason": "a facts file names this machine; it is not another machine's and is ignored"})
            continue
        held = newest.get(peer["machine"])
        if held is not None:
            degraded.append({"source": "peers", "code": "duplicate-machine",
                             "reason": f"two facts files name {peer['machine']}; the newer is used"})
            if _parse_time(held["generated_at"]) >= _parse_time(peer["generated_at"]):
                continue
        newest[peer["machine"]] = dict(peer)
    others, fresh = [newest[m] for m in sorted(newest)], []
    for peer in others:
        when = _parse_time(peer["generated_at"])
        age = (at - when) if when else None
        peer["age_hours"] = round(age.total_seconds() / 3600, 1) if age is not None else None
        if when is not None and when - at > CLOCK_SKEW:
            peer["stale"] = "dated in the future: check both clocks"
        elif age is None or age.total_seconds() > max_age_hours * 3600:
            peer["stale"] = f"older than {max_age_hours:g} hours"
        else:
            fresh.append(peer)
    def said(peer):
        return f"{peer['machine']} (facts of {peer['generated_at']})"
    carry = (f"on the other machine: project-observatory full machine-move facts --out FILE, then here: "
             f"project-observatory full machine-move status --peer FILE")
    items = []

    # M1: the same release on both.
    ours = config.VERSION
    if not fresh:
        items.append(_item("release", UNKNOWN, [f"this machine runs {ours}; no facts of another machine younger "
                                                f"than {max_age_hours:g} hours"
                                                + "".join(f"; {said(p)} {p['stale']}" for p in others)], [carry]))
    else:
        differ = [p for p in fresh if p.get("engine_version") != ours]
        evidence = [f"this machine runs {ours}"] + [f"{said(p)} runs {p.get('engine_version') or 'an unknown release'}"
                                                    for p in fresh]
        items.append(_item("release", MISSING if differ else OK, evidence,
                           ["project-observatory full update --apply  (on the machine with the older release)"]
                           if differ else None))

    # M2: the restore complete.
    receipt, receipt_problem = _receipt(home)
    mine = restore_summary(receipt)
    theirs = [p for p in others if me and p.get("restore") and p["restore"].get("taken_on") == me]
    if receipt_problem:
        items.append(_item("restore", UNKNOWN, [receipt_problem]))
    elif mine:
        evidence = [f"machine-restore applied here at {mine['at']} ({mine['mode']}), from "
                    f"{mine['taken_on'] or 'a bundle that names no machine'}"]
        if mine["left_out"]:
            evidence.append(f"left out on purpose (--only/--skip): {', '.join(mine['left_out'])}")
        evidence += mine["gaps"]
        if not mine["cloned"]:
            evidence.append("--clone has not run here (the checkouts are not restored)")
        steps = []
        if any("conflict" in g for g in mine["gaps"]):
            steps.append("project-observatory full machine-restore --latest --apply --overwrite  (replaces each "
                         "conflict, moving it aside)")
        if any("failed" in g for g in mine["gaps"]) or not mine["cloned"]:
            steps.append("project-observatory full machine-restore --latest --apply --clone  (writes what is still "
                         "missing; the answer names each failure)")
        items.append(_item("restore", OK if mine["complete"] and mine["cloned"] else MISSING, evidence, steps or None))
    elif theirs:
        peer = theirs[0]
        state = OK if peer["restore"]["complete"] else MISSING
        items.append(_item("restore", state, [f"{said(peer)} restored a bundle of this machine at "
                                              f"{peer['restore']['at']}: "
                                              + ("complete" if state == OK else "; ".join(peer["restore"]["gaps"])
                                                 or "not complete")],
                           None if state == OK else ["on that machine: project-observatory full machine-move status"]))
    else:
        items.append(_item("restore", UNKNOWN, ["no machine-restore was recorded on this machine, and no other "
                                                "machine's facts say it restored a bundle of this one"],
                           ["on the machine you moved to: project-observatory full machine-restore --latest --apply"]))

    # M3: its own SSH key.
    keys, key_notes = own_keys(home)
    other_fps: dict[str, str] = {}
    for peer in others:
        for k in peer["ssh"]:
            other_fps.setdefault(k["fingerprint"], f"{k['file']} on {said(peer)}")
    applied = (receipt or {}).get("apply")
    withheld = applied.get("other_keys") if isinstance(applied, dict) else None
    for k in withheld if isinstance(withheld, list) else []:
        if isinstance(k, dict) and isinstance(k.get("fingerprint"), str) and FINGERPRINT.fullmatch(k["fingerprint"]):
            other_fps.setdefault(k["fingerprint"], f"the key {str(k.get('key'))[:64]} the restored bundle withheld")
    shared = [k for k in keys if k["fingerprint"] and k["fingerprint"] in other_fps]
    if not keys:
        # The same two steps the restore hands out (OBS-300, OBS-306): rekey makes this machine's key under
        # the other machine's key's name and sends it where that key is registered, matched on GitHub by
        # its fingerprint. Only what the receipt and the peers' facts name is offered; nothing is guessed.
        known = _other_key_names(withheld, others)
        evidence = ["no private SSH key in ~/.ssh on this machine", *key_notes]
        if known:
            steps = [s for name, fp in known for s in _rekey_steps(name, fp)]
        else:
            evidence.append("nothing here names the other machine's keys directly in ~/.ssh (no restore receipt "
                            "that lists them, no other machine's facts)")
            steps = ["on the other machine: project-observatory full ssh-keys list  (each key's name and SHA256 "
                     "fingerprint)",
                     "project-observatory full ssh-keys rekey NAME --fingerprint SHA256:...  (the plan), then the "
                     "same with --apply"]
        items.append(_item("ssh-key", MISSING, evidence, steps))
    elif shared:
        steps, notes, handed = [], [], set()
        folder = home / ".ssh"
        try:
            taken = _ssh_names(folder)
        except OSError as exc:
            taken = set()
            notes.append(f"~/.ssh could not be listed ({type(exc).__name__}): the new key names below were not "
                         "checked against the files there; `project-observatory full ssh-keys list` shows them")

        def mine(name):
            fp = _own_pair(home, name)
            return fp if fp and fp not in other_fps else None
        for k in shared:
            old = k["file"][len("~/.ssh/"):] if k["file"].startswith("~/.ssh/") else ""
            if not KEY_REF.fullmatch(old) or old.endswith(".pub"):
                steps.append(f"{k['file']} is not a key name rekey takes (letters, digits, . _ -, in ~/.ssh or "
                             "one folder below): make this machine's own key under a new name (project-observatory "
                             f"full ssh-keys rekey NEW --fingerprint {k['fingerprint']}, then the same with --apply) "
                             "and remove the copy by hand")
                continue
            # The copy here holds the old name, so this machine's own key takes a new one (rekey refuses to
            # reuse a key whose fingerprint is the other machine's).
            new, made = _new_key_name(old, taken, handed, mine)
            if made:
                notes.append(f"~/.ssh/{new} ({made}) is already this machine's own key: the rekey steps reuse it "
                             "and send it only where it is still missing")
            steps += _rekey_steps(old, k["fingerprint"], new)
            if old in DEFAULT_IDENTITIES:
                # ssh offers a default name to every host whose config names no key (plain git@github.com
                # among them) and never offers ~/.ssh/NEW unless a line names it (review of #414). rekey
                # cannot know those hosts: they got the new key only when named to it with --server.
                steps.append(f"ssh offered ~/.ssh/{old} to every host whose config names no key, and rekey cannot "
                             f"know those hosts: each got ~/.ssh/{new} only if it was added to both rekey steps with "
                             f"--server ALIAS (run them again with it if not). Then, before removing the copy, add "
                             f"`IdentityFile ~/.ssh/{new}` for github.com and for each of those hosts (or once under "
                             f"`Host *`): ssh offers ~/.ssh/{new} only where a line names it; point each Host that "
                             f"names ~/.ssh/{old} at ~/.ssh/{new} too, then remove the copy ~/.ssh/{old} by hand: "
                             "rekey removes nothing")
            else:
                steps.append(f"then point each Host that names ~/.ssh/{old} at IdentityFile ~/.ssh/{new} and "
                             f"remove the copy ~/.ssh/{old} by hand: rekey removes nothing")
        items.append(_item("ssh-key", MISSING,
                           [f"{k['file']} ({k['fingerprint']}) is the same key as {other_fps[k['fingerprint']]}"
                            for k in shared] + notes + key_notes, steps))
    elif not other_fps:
        items.append(_item("ssh-key", UNKNOWN, [f"{len(keys)} private key(s) here; nothing here says which keys the "
                                                "other machine holds", *key_notes], [carry]))
    else:
        unread = [k["file"] for k in keys if not k["fingerprint"]]
        evidence = [f"{len(keys)} private key(s) here, none of them among the other machine's {len(other_fps)} "
                    f"key(s)"] + ([f"fingerprint not read (no public half): {', '.join(unread)}"] if unread else [])
        items.append(_item("ssh-key", UNKNOWN if unread and len(unread) == len(keys) else OK, evidence + key_notes))

    # M3: its own device id.
    device, why = device_id(base)
    known = [p for p in others if p.get("device")]
    if device is None:
        items.append(_item("device-id", MISSING, [why or "no device id"],
                           ["project-observatory full node --init  (this machine's own device id)"]))
    elif any(p["device"] == device for p in known):
        twin = next(p for p in known if p["device"] == device)
        items.append(_item("device-id", MISSING, [f"this machine's device id {device} is also {said(twin)}'s: its "
                                                  "workspace was restored whole"],
                           ["restore the workspace snapshot as a second machine (`project-observatory full restore "
                            "--latest --second-machine` into a new home), then `full node --init`"]))
    elif not known:
        items.append(_item("device-id", UNKNOWN, [f"this machine's device id is {device}; no other machine's facts "
                                                  "name one to compare"], [carry]))
    else:
        items.append(_item("device-id", OK, [f"this machine's device id is {device}"]
                           + [f"{said(p)} has {p['device']}" for p in known]))

    # M3: every vault slot the same.
    view, problem = (vault_view or _vault_view)(max_age_hours)
    if view is None:
        items.append(_item("vault", UNKNOWN, [problem or "the vault digests were not compared"]))
    elif view.get("all_match"):
        items.append(_item("vault", OK, [view.get("summary") or "every slot matches"]))
    elif not [m for m in view.get("machines", []) if m.get("state") == "reachable"]:
        items.append(_item("vault", UNKNOWN, [view.get("summary") or "no other machine's digests to compare"],
                           ["on the other machine: project-observatory full vault-digests write, then put its "
                            "local.json in this machine's vault-digests peers folder"]))
    else:
        items.append(_item("vault", MISSING, [view.get("summary") or "some slots differ"],
                           ["project-observatory full vault-digests  (names each slot that differs, is missing or "
                            "is only on one machine)"]))

    # M4: no job on both machines.
    jobs, jobs_problem = defined_jobs(home, platform)
    try:
        elsewhere = {e["label"]: e for e in mi.read_placement(home)["elsewhere"]}
    except mi.MachineIdentityError as exc:
        elsewhere, jobs_problem = {}, jobs_problem or str(exc)
    space = _space_evidence(home, platform)
    if jobs is None:
        items.append(_item("jobs", UNKNOWN, [jobs_problem or "the job folder could not be read"]))
    else:
        both = [f"{label} is defined here and named in this machine's placement file as running on "
                f"{elsewhere[label].get('placedOn') or 'another machine'}" for label in jobs if label in elsewhere]
        fresh_jobs = [p for p in fresh if isinstance(p.get("jobs"), list)]
        for peer in fresh_jobs:
            both += [f"{label} is defined here and on {said(peer)}" for label in jobs if label in set(peer["jobs"])]
        if both:
            items.append(_item("jobs", MISSING, both + space,
                               ["project-observatory full machine-move leave LABEL --to MACHINE_ID  (on the machine "
                                "the job leaves), then `machine-move cutover LABEL` on the other"]))
        elif not fresh_jobs:
            items.append(_item("jobs", UNKNOWN, [f"{len(jobs)} job(s) defined here; no facts of another machine "
                                                 f"younger than {max_age_hours:g} hours list its jobs"] + space,
                               [carry]))
        else:
            items.append(_item("jobs", OK, [f"{len(jobs)} job(s) defined here, {len(elsewhere)} placed on another "
                                            "machine; none is defined on both"]
                               + [f"{said(p)} defines {len(p['jobs'])}" for p in fresh_jobs] + space))

    counts = {s: sum(1 for i in items if i["state"] == s) for s in (OK, MISSING, UNKNOWN)}
    all_ok = counts[OK] == len(items)
    summary = ("The move is done: " if all_ok else "The move is not done yet: ") + \
        f"{counts[OK]} of {len(items)} items ok, {counts[MISSING]} missing, {counts[UNKNOWN]} unknown."
    return {"schema": STATUS_SCHEMA, "machine": me, "generated_at": _iso(at), "engine_version": ours,
            "other_machines": [{"machine": p["machine"], "generated_at": p["generated_at"], "age_hours": p["age_hours"],
                                **({"stale": p["stale"]} if p.get("stale") else {})} for p in others],
            "items": items, "counts": counts, "all_ok": all_ok, "summary": summary, "degraded": degraded}


def render(view: dict) -> str:
    lines = [view["summary"], f"  this machine: {view['machine'] or 'unknown'}, release {view['engine_version']}"]
    for peer in view["other_machines"]:
        lines.append(f"  other machine: {peer['machine']}, facts of {peer['generated_at']}"
                     + (f" ({peer['stale']})" if peer.get("stale") else ""))
    for item in view["items"]:
        lines.append(f"  {item['step']} {item['id']:<9} {item['state']:<7} {item['title']}")
        for line in item["evidence"]:
            lines.append(f"      {line}")
        for step in item.get("next") or []:
            lines.append(f"      next: {step}")
    for d in view["degraded"]:
        lines.append(f"  degraded: {d['reason']}")
    return "\n".join(lines)


def write_facts(doc: dict, out: Path) -> Path:
    """A new owner-only file, whole or not at all, that never replaces one (it may be another machine's facts):
    written to a temporary file beside it, then linked to its name, which fails when the name exists (a link
    included). A file system without hard links gets the exclusive create instead."""
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if os.path.lexists(out):
        raise FileExistsError(str(out))
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, out)
        except FileExistsError:
            raise
        except (OSError, NotImplementedError):
            exclusive = osprivacy.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW, 0o600)
            with os.fdopen(exclusive, "wb") as stream:
                stream.write(data)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return out


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"{PROG} machine-move", description="move one background job between two machines, so it "
                                                        "never runs on both (the placement file says where it runs)")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="this machine's id, the placement file and the jobs staged here")
    cut = sub.add_parser("cutover", help="start a staged job here and remove it from this machine's `elsewhere`")
    cut.add_argument("label")
    out = sub.add_parser("leave", help="stop a job here, stage it, and name it as running on another machine")
    out.add_argument("label")
    out.add_argument("--to", required=True, metavar="MACHINE_ID", help="the id `machine-move show` prints there")
    out.add_argument("--via", metavar="USER@HOST",
                     help="macOS: also forward the service's local port (its descriptor's http://127.0.0.1:PORT) to "
                          "this account on the other machine, through one launchd ssh job (OBS-480)")
    back = sub.add_parser("return", help="the move back, first step here: remove the forward `leave --via` set up "
                                         "for a job, so the service can take its port again at cutover")
    back.add_argument("label")
    back.add_argument("--remove-adopted", action="store_true",
                      help="also unload a forward set up by hand that leave adopted, and set its plist aside")
    st = sub.add_parser("status", help="the checklist of a move, M1 to M4: one release, the restore complete, "
                                       "its own SSH key and device id, the vault the same, no job on both")
    st.add_argument("--json", action="store_true", help="the checklist as JSON")
    st.add_argument("--peer", action="append", type=Path, metavar="FILE",
                    help="the other machine's facts (`machine-move facts --out FILE` there); default: every "
                         "*.json in <data folder>/machine-move/peers/")
    st.add_argument("--max-age-hours", type=_hours, default=MAX_AGE_HOURS, metavar="N",
                    help=f"facts older than this say nothing about the release or the jobs (default {MAX_AGE_HOURS})")
    fa = sub.add_parser("facts", help="this machine's public facts for the other machine's `machine-move status`")
    fa.add_argument("--out", type=Path, metavar="FILE", help="write them to a new owner-only file (default: print)")
    return ap


def _hours(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a number of hours") from None
    if not 0 < value <= 24 * 366:
        raise argparse.ArgumentTypeError("between 0 and 8784 hours")
    return value


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    home = Path.home()
    try:
        if a.action == "status":
            import machine_backup
            base = machine_backup._home_or_none()
            peers, bad = load_peers(home, [p.expanduser() for p in a.peer] if a.peer else None)
            view = status(home, base, peers=peers, unreadable=bad, max_age_hours=a.max_age_hours)
            print(json.dumps(view, indent=2, ensure_ascii=False) if a.json else render(view))
            return 0 if view["all_ok"] else 1
        if a.action == "facts":
            import machine_backup
            doc = facts(home, machine_backup._home_or_none())
            o = doc["omitted"]
            if any(o.values()):
                print(f"Observatory: {o['ssh_names_masked']} key file name(s) masked (the fingerprint kept), "
                      f"{o['jobs_left_out']} job label(s) and {o['elsewhere_left_out']} placed label(s) left out: "
                      "they do not fit the facts format the other machine reads (`omitted` in the answer)",
                      file=sys.stderr)
            if a.out is None:
                print(json.dumps(doc, indent=2, ensure_ascii=False))
                return 0
            try:
                path = write_facts(doc, a.out.expanduser())
            except FileExistsError:
                raise MachineMoveError(f"{a.out} already exists; facts are written to a new file") from None
            print(f"wrote {path}: carry it to the other machine and run `project-observatory full machine-move "
                  f"status --peer FILE` there")
            return 0
        if a.action == "show":
            result = show(home)
        elif a.action == "cutover":
            result = cutover(a.label, home)
        elif a.action == "return":
            result = return_job(a.label, home, platform=sys.platform, remove_adopted=a.remove_adopted)
        else:
            result = leave(a.label, a.to, home, platform=sys.platform, via=a.via)
    except (MachineMoveError, mi.MachineIdentityError, OSError, EOFError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, (OSError, MachineMoveFailed)) else 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    forward = result.get("forward") if isinstance(result.get("forward"), dict) else {}
    if forward.get("state") in ("refused", "failed"):
        # The job moved; the forward did not: exit 1 (something changed), its reason on stderr too.
        print(f"Observatory: the job moved, the forward did not: {forward.get('message')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
