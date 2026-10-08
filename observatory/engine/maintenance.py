"""Updates that arrive by themselves, and a daily backup that leaves the workspace.

The decisions behind it are in docs/runs/2026-10-05-auto-update (D1–D8). One job per
workspace runs `tools/maintain.py run` every hour — launchd on macOS, a systemd user
timer on Linux — and each pass does only what is due:

1. a backup passphrase exists and is kept outside the workspace (backup_vault.SecretStore);
2. every six hours, with automatic updates on (the default; the switch is below):
   `full update --check`, and when a newer stable release exists, `full update --apply
   --unattended` — verified and reversible, the same transaction a person runs — but only
   at a safe point: while the local server has answered no client and no memory-http
   client has called in the last five minutes (`live_clients`); otherwise the update waits
   for the next pass. A release that declares a step needing a person is verified and
   held, never installed by the job. A check that could not look is retried once within
   the hour; a failed update is retried at the next check, sooner when the reason was
   momentary; a rollback that needs a person stops the automatic attempts until one
   succeeds;
3. macOS: the app follows the engine (app_update.py), never while it runs;
4. once a day: a full encrypted snapshot of the workspace (settings, registry, vault,
   store), with this workspace's tick and server stopped for the copy.

The scheduler runs the pass hourly and at load; launchd passes `--first-check-delay 90`
so the first check waits 90 seconds after the job starts (a login, a boot), and the
systemd timer starts 90 seconds after the user manager does (lifecycle LC-16).

THE SWITCH (LC-16) is the file `auto-update` in the workspace home: absent means on, and
only the word `off` turns automatic updates off. `full auto-update on|off` writes it; an
update, a reinstall or an uninstall never does. Installs that turned updates off before
0.19.0 did it with `updates.auto: false` in config/settings.json: that is still honoured
as off while the file is absent, and the next `auto-update` command moves it into the file
and says so. Off stops the checks, the downloads and the installs, the app's included;
the daily backup keeps running.

    full auto-update status|on|off     the switch; off keeps the backups
    full maintain run                  one pass now (what the scheduler runs)
    full maintain ensure               schedule the pass and mirror the passphrase
    full maintain uninstall            remove the schedule; it stays off until `ensure`
    full maintain status               what ran, when, and what it found
    full maintain app                  the app step alone: swap a staged app now (the
                                       Mac app runs it after it quits — LC-16 activation)
    full restore --latest              restore the newest backup of a workspace at this path

The maintenance job is deliberately NOT one of install_launchd.managed_jobs(): those are
the jobs `full update` stops, and stopping this one would kill the process running the
update (D3). Nothing here prints or logs a secret.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import configuration as config
import backup_vault
import update_events

STATE_FILE = "maintenance.json"
#: How often a newer release is looked for (lifecycle LC-16: every six hours).
CHECK_EVERY = datetime.timedelta(hours=6)
#: A check that could not look (network, rate limit, a timeout on a loaded machine) is
#: tried once more within the hour, then back to CHECK_EVERY (LC-16).
RETRY_UNDETERMINED = datetime.timedelta(hours=1)
#: The scheduler's first check waits this long after the job started (LC-16: 90 s).
FIRST_CHECK_DELAY = 90
#: The switch: a file in the workspace home (LC-16). Absent = on; only `off` turns it off.
SWITCH_FILE = "auto-update"
SWITCH_LABEL = "Install updates automatically"
#: How recent a client must be to count as live: the server's own CLIENT_WINDOW_SECONDS.
CLIENT_WINDOW = datetime.timedelta(minutes=5)
#: When this process started; the first-check delay is measured from it.
PROCESS_STARTED = time.monotonic()
#: Why an update can be refused for a moment rather than for good: a copy torn or changed
#: by a concurrent writer, a busy lock, another update. These are retried the next hour.
TRANSIENT = ("integrity verification failed", "disk image is malformed", "changed during snapshot",
             "is busy", "Another `full update --apply` is running", "TimeoutExpired",
             "still holds the lock", "another update")


def _transient(record: dict) -> bool:
    detail = str(record.get("detail") or "")
    if record.get("result") == "another-update-running":
        return True
    return record.get("result") in ("refused", "failed-rolled-back", "undetermined") and any(
        marker in detail for marker in TRANSIENT)


#: The journal events that end a `full update --apply` (engine_update.Transaction).
FINAL_EVENTS = {"updated": "updated", "rolled-back": "failed-rolled-back",
                "rollback-failed": "needs-person", "refused": "refused", "held": "held"}


def reconcile_in_flight(base: Path, state: dict) -> dict | None:
    """An apply the previous pass started and did not see end (the pass was stopped while
    the update ran on, in its own session). Its outcome is read from the update journal
    once the update lock is free (audit A03)."""
    flight = state.get("update_in_flight")
    if not flight or update_running(base):
        return None
    since = parse_iso(flight.get("at"))
    outcome = None
    try:
        lines = (base / "store" / "logs" / "update.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        when = parse_iso(row.get("at"))
        if since and when and when < since:
            break
        if row.get("event") == "undo-ended":
            outcome = {"at": flight.get("at"), "reconciled": True, "from": row.get("from"), "to": row.get("to"),
                       "result": "needs-person" if row.get("outcome") == "needs-person" else "failed-rolled-back"}
            if row.get("error"):
                outcome["detail"] = str(row["error"])[:300]
            break
        if row.get("event") in FINAL_EVENTS:
            outcome = {"at": flight.get("at"), "result": FINAL_EVENTS[row["event"]],
                       "from": row.get("from"), "to": row.get("to"), "reconciled": True}
            if row.get("needs_person"):
                outcome["needs_person"] = str(row["needs_person"])[:1000]
            if row.get("error"):
                outcome["detail"] = str(row["error"])[:300]
            break
    state.pop("update_in_flight", None)
    if outcome is None:
        outcome = {"at": flight.get("at"), "result": "failed", "reconciled": True,
                   "detail": "the update ended without a final journal entry"}
    state["update"] = outcome
    return outcome


SNAPSHOT_EVERY = datetime.timedelta(hours=24)
INTERVAL_SECONDS = 3600
#: Exit codes of `full update` (engine_update.EXIT_*), named here so a change there is
#: caught by tests/test_maintenance.py rather than by a user's stalled install.
UPDATE_OK, UPDATE_FAILED, UPDATE_REFUSED, UPDATE_UNDETERMINED = 0, 1, 2, 3
UPDATE_NEEDS_PERSON, UPDATE_SERVICES, UPDATE_HELD, UPDATE_AVAILABLE = 4, 5, 6, 10
SNAPSHOT_ATTEMPTS = 3
#: Set in every process a maintenance pass starts. A schedule installed from inside a pass
#: never boots the job out: that would kill the pass and whatever it runs (review F1).
IN_PASS_ENV = "OBSERVATORY_MAINTENANCE_PASS"
SNAPSHOT_KIND = "daily"


# --- the switch -------------------------------------------------------------------

def settings(base: Path) -> dict:
    return config.load(base).get("updates", {})


def switch_path(base: Path) -> Path:
    return base / SWITCH_FILE


def read_switch(base: Path) -> str | None:
    """`on` or `off` from the switch file, or None when there is none.

    Only the word `off` (any case, surrounding space ignored) turns updates off; anything
    else a person wrote reads as on. A link or a folder in its place is not a switch."""
    file = switch_path(base)
    try:
        if file.is_symlink() or not file.is_file():
            return None
        text = file.read_text(encoding="utf-8", errors="replace")[:64]
    except OSError:
        return None
    return "off" if text.strip().lower() == "off" else "on"


def legacy_off(base: Path) -> bool:
    """`updates.auto: false` in settings.json — how 0.17 and 0.18 turned updates off."""
    return settings(base).get("auto", True) is False


def auto_enabled(base: Path) -> bool:
    """On unless a person turned it off: the switch file decides; without one, an older
    install's `updates.auto: false` still reads as off (D1, LC-16)."""
    word = read_switch(base)
    if word is not None:
        return word != "off"
    return not legacy_off(base)


def switch_status(base: Path) -> dict:
    word = read_switch(base)
    source = "file" if word is not None else "settings" if legacy_off(base) else "default"
    out = {"label": SWITCH_LABEL, "on": auto_enabled(base), "file": str(switch_path(base)), "source": source}
    if source == "settings":
        out["note"] = ("off by `updates.auto: false` in config/settings.json; the next "
                       "`project-observatory full auto-update status` (or `on`, `off`) moves it into the file")
    return out


def write_switch(base: Path, on: bool) -> None:
    """The person's choice, written to the file (owner-only, atomic, never through a link)."""
    import workspace
    file = switch_path(base)
    workspace.reject_symlinks(file)
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".auto-update-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("on\n" if on else "off\n")
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def migrate_legacy(base: Path) -> str | None:
    """Move `updates.auto` out of settings.json into the switch file; what happened, or None.

    Run by every `auto-update` command. A false setting becomes `off` in the file (unless a
    file already says otherwise); the key is then removed so the two never disagree."""
    import workspace
    with workspace.lock(base):
        doc = config.load(base)
        updates = doc.get("updates") or {}
        if "auto" not in updates:
            return None
        was = updates.pop("auto")
        had_file = read_switch(base) is not None
        if was is False and not had_file:
            write_switch(base, False)
        if not updates:
            doc.pop("updates", None)
        workspace.write_json(base / "config" / "settings.json", doc)
    if was is False and not had_file:
        return (f"`updates.auto: false` in config/settings.json was moved to {switch_path(base)} (off); "
                "the file is the switch from now on")
    return "`updates.auto` was removed from config/settings.json; the file `auto-update` is the switch"


def schedule_wanted(base: Path) -> bool:
    """`updates.scheduled` is on unless `maintain uninstall` turned it off."""
    return settings(base).get("scheduled", True) is not False


def set_setting(base: Path, name: str, value: bool) -> None:
    import workspace
    if name not in config.UPDATE_SETTINGS:
        raise config.ConfigurationError(f"Unknown updates setting: {name}")
    with workspace.lock(base):
        doc = config.load(base)
        doc.setdefault("updates", {})[name] = bool(value)
        workspace.write_json(base / "config" / "settings.json", doc)


# --- state ------------------------------------------------------------------------

def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def state_path(base: Path) -> Path:
    return base / "store" / STATE_FILE


def read_state(base: Path) -> dict:
    file = state_path(base)
    if not file.is_file() or file.is_symlink():
        return {}
    try:
        doc = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state(base: Path, doc: dict) -> None:
    file = state_path(base)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".maintenance-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(doc, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def due(state: dict, key: str, every: datetime.timedelta, at: datetime.datetime) -> bool:
    last = parse_iso((state.get(key) or {}).get("at"))
    return last is None or at - last >= every


# --- system effects, guarded -------------------------------------------------------

def system_setup_allowed(base: Path) -> bool:
    """Whether this process may touch the machine: launchd, systemd, the Keychain.

    Explicit `OBSERVATORY_SYSTEM_SETUP=1` (the scheduler, the plugin hook, `full update`)
    says yes and `=0` says no. Otherwise only a person at a terminal, and never for a
    workspace inside the temporary directory — the suites create hundreds of those, and a
    test that ran `init` from a developer's terminal must not schedule a job or write a
    Keychain item for a home that is about to disappear."""
    flag = os.environ.get("OBSERVATORY_SYSTEM_SETUP")
    if flag == "1":
        return True
    if flag == "0":
        return False
    # A $HOME that is not this account's own home is a test's or a sandbox's (audit A07):
    # launchd, the Keychain and systemd would still be the real ones.
    if not _real_home():
        return False
    try:
        temp = Path(tempfile.gettempdir()).resolve()
        resolved = base.resolve()
        if resolved == temp or temp in resolved.parents \
                or any(root in resolved.parents for root in SYSTEM_TEMP_ROOTS):
            return False
    except OSError:
        return False
    return sys.stdin is not None and sys.stdin.isatty()


#: Where the operating system keeps temporary directories, whatever TMPDIR this process was
#: handed. A test runner gives each child its own TMPDIR beside the workspace it builds
#: (`base/tmp` next to `base/runtime`), so the child's `gettempdir()` is not an ancestor of
#: that workspace — but the runner's own temporary directory is, and on macOS that lives
#: under /var/folders (audit A24: the guard's test patched `gettempdir` in-process and could
#: not see this). No real workspace lives under any of these.
SYSTEM_TEMP_ROOTS = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"), Path("/private/var/tmp"),
                     Path("/var/folders"), Path("/private/var/folders"))


def _real_home() -> bool:
    import pwd
    try:
        return Path.home().resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    except (KeyError, OSError):
        return False


def probes_allowed() -> bool:
    """Read-only probes of launchd/systemd (status, doctor, the dashboard) — skipped when
    OBSERVATORY_SYSTEM_SETUP=0 says this process must not touch the machine (audit A25)."""
    return os.environ.get("OBSERVATORY_SYSTEM_SETUP") != "0" and _real_home()


def engine_python() -> str:
    """The interpreter this engine is installed in (`configuration.engine_python`)."""
    return config.engine_python()


class Commands:
    """The engine's own CLI, run in a new process with the installed code."""

    def __init__(self, base: Path, python: str | None = None, runner=subprocess.run):
        self.base, self.python, self.runner = base, python or engine_python(), runner

    def apply(self, *args: str, env: dict | None = None) -> tuple[int, dict, str]:
        """`full update --apply …`, in a session of its own with its output in a file.

        Never given a timeout and never killed: a SIGTERM to the pass, a launchd bootout or
        the pass's own end must not stop pip or the new release's upgrade half-way, which
        would leave the jobs it stopped down (audit A03). `update_lock` keeps it single."""
        environ = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "OBSERVATORY_ROOT"}}
        environ.update(env or {})
        environ[IN_PASS_ENV] = "1"
        # BESIDE the home, next to the update lock, never inside it: a rollback renames the
        # home to `.failed-update-*` and puts the snapshot in its place, and output written
        # inside followed the old copy, so the pass read an empty report (A03 review).
        out_path, err_path = apply_output_paths(self.base)
        out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with _private_file(out_path) as out, _private_file(err_path) as err:
                proc = subprocess.Popen(
                    [self.python, "-P", "-m", "observatory", "--home", str(self.base), "full", "update", *args],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=environ,
                    cwd=tempfile.gettempdir(), start_new_session=True)
            code = proc.wait()
        except OSError as exc:
            return 125, {}, type(exc).__name__
        try:
            text = out_path.read_text(encoding="utf-8")
            doc = json.loads(text) if text.strip() else {}
        except (OSError, ValueError):
            doc = {}
        try:
            tail = err_path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            tail = []
        return code, doc if isinstance(doc, dict) else {}, (tail[-1] if tail else "")[:300]

    def full(self, *args: str, timeout: int = 3600, env: dict | None = None) -> tuple[int, dict, str]:
        environ = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "OBSERVATORY_ROOT"}}
        environ.update(env or {})
        environ[IN_PASS_ENV] = "1"
        try:
            p = self.runner([self.python, "-P", "-m", "observatory", "--home", str(self.base), "full", *args],
                            capture_output=True, text=True, timeout=timeout, env=environ,
                            cwd=tempfile.gettempdir())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, {}, type(exc).__name__
        try:
            doc = json.loads(p.stdout) if p.stdout.strip() else {}
        except ValueError:
            doc = {}
        tail = (p.stderr or "").strip().splitlines()
        return p.returncode, doc if isinstance(doc, dict) else {}, (tail[-1] if tail else "")[:300]


def apply_output_paths(base: Path) -> tuple[Path, Path]:
    """Where an automatic apply's stdout and stderr go: beside the workspace, as the
    update lock is (engine_update._update_lock_path), so a rollback's swap of the home
    cannot carry them away."""
    return (base.parent / f".{base.name}.update-apply.out", base.parent / f".{base.name}.update-apply.err")


def _private_file(path: Path):
    """Truncated, owner-only, and never through a symbolic link."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


# --- one pass ---------------------------------------------------------------------

@contextlib.contextmanager
def pass_lock(base: Path):
    """One pass at a time per workspace; a second one finds it busy and leaves."""
    file = base / "store" / "maintenance.lock"
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def pass_running(base: Path) -> bool:
    """True while a maintenance pass holds its lock (this process's or another's)."""
    if os.environ.get(IN_PASS_ENV) == "1":
        return True
    file = base / "store" / "maintenance.lock"
    if not file.exists():
        return False
    fd = os.open(file, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def update_running(base: Path) -> bool:
    """True while a `full update --apply` holds its lock (engine_update.update_lock)."""
    import engine_update
    return engine_update.update_lock_held(base)


def live_clients(base: Path, at: datetime.datetime) -> list[str]:
    """Why activating a new release now would interrupt someone; empty when it would not.

    Installing an update restarts this workspace's tick and local server on the new code
    (that is its activation). A person reading the dashboard, or an agent asking the
    server, is a live client of it: tools/serverd.py records in its receipt whether any
    request other than a host's probe (`/health`, the service document, the events feed)
    arrived in its last CLIENT_WINDOW_SECONDS. A memory-http caller is journalled in
    store/logs/access.jsonl. A receipt older than the age the server itself calls silent
    is a server that is not running, and so has no client to interrupt."""
    reasons = []
    receipt = base / "store" / "raw" / "serverd.json"
    try:
        doc = json.loads(receipt.read_text(encoding="utf-8")) if receipt.is_file() and not receipt.is_symlink() else {}
    except (OSError, ValueError):
        doc = {}
    if isinstance(doc, dict):
        when = parse_iso(doc.get("at"))
        silent = doc.get("silent_after_s") if type(doc.get("silent_after_s")) is int else 480
        clients = doc.get("clients") if isinstance(doc.get("clients"), dict) else {}
        if when is not None and abs((at - when).total_seconds()) <= silent and clients.get("recent") is True:
            reasons.append("the local server answered a client in the last five minutes")
    journal = base / "store" / "logs" / "access.jsonl"
    try:
        if journal.is_file() and not journal.is_symlink():
            with open(journal, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - 65536))
                tail = fh.read().decode("utf-8", errors="replace").splitlines()
        else:
            tail = []
    except OSError:
        tail = []
    for line in reversed(tail):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        when = parse_iso(row.get("at")) if isinstance(row, dict) else None
        if when is None:
            continue
        if at - when > CLIENT_WINDOW:
            break
        # Only allowed calls by a binding: the local stdio agent's allowed calls are never
        # journalled, and a refused caller is not a session an update would interrupt.
        if row.get("allowed") is True and row.get("binding"):
            reasons.append("a memory-http client called in the last five minutes")
            break
    return reasons


def _engine_moved_past(target) -> bool:
    try:
        return config.version_tuple(config.VERSION) >= config.version_tuple(str(target))
    except (ValueError, TypeError, config.ConfigurationError):
        return False


def step_update(base: Path, state: dict, at: datetime.datetime, commands: Commands,
                before_check=None) -> dict:
    """Check every six hours; apply a newer stable release at a safe point. Returns what happened."""
    if not auto_enabled(base):
        return {"result": "off"}
    reconcile_in_flight(base, state)
    last = state.get("update") or {}
    if last.get("failures") and last.get("from") != config.VERSION:
        # The engine moved on (a person updated, or a later automatic update worked):
        # earlier failures are history, not a warning (audit A13).
        state["update"] = last = {k: v for k, v in last.items() if k != "failures"}
    if last.get("result") == "needs-person" and last.get("from") != config.VERSION:
        # A person ran `full update` (or reinstalled): the engine is no longer the one that
        # needed them, so the automatic attempts resume (review F3).
        state["update"] = {**last, "result": "resolved-by-person", "resolved_at": iso(at)}
        last = state["update"]
    if last.get("result") == "held" and _engine_moved_past(last.get("to")):
        # The person did the step and installed the held release (or a later one).
        state["update"] = {**last, "result": "resolved-by-person", "resolved_at": iso(at)}
        last = state["update"]
    if last.get("result") == "needs-person":
        return {"result": "waiting-for-person",
                "detail": "the last automatic update could not roll back by itself; "
                          "`project-observatory full update` says what to do"}
    last_check, last_update = state.get("check") or {}, state.get("update") or {}
    same_pass = last_update.get("at") == last_check.get("at")
    # One retry within the hour after a check that could not look, then every six hours
    # (LC-16); an update deferred for a live client or refused for a moment is tried at
    # the next hourly pass.
    hourly = (last_check.get("result") == "undetermined" and int(last_check.get("failures") or 1) <= 1) or (
        same_pass and (last_update.get("result") == "deferred" or _transient(last_update)))
    every = RETRY_UNDETERMINED if hourly else CHECK_EVERY
    if not due(state, "check", every, at):
        return {"result": "not-due"}
    if before_check is not None:
        before_check()
    code, report, err = commands.full("update", "--check", timeout=600)
    check = {"at": iso(at), "exit": code}
    if code == UPDATE_OK:
        check["result"] = "up-to-date"
    elif code == UPDATE_AVAILABLE:
        check["result"] = "update-available"
        check["latest"] = (report.get("target") or {}).get("version") if isinstance(report.get("target"), dict) \
            else report.get("target")
    else:
        check["result"] = "undetermined"
        check["detail"] = err or f"exit {code}"
        check["failures"] = int(last_check.get("failures") or 0) + 1 if last_check.get("result") == "undetermined" else 1
    state["check"] = check
    if code != UPDATE_AVAILABLE:
        return {"result": check["result"]}
    if last.get("result") == "held" and check.get("latest") == last.get("to"):
        # Downloaded and verified once already; the step is still the person's.
        state["update"] = {**last, "checked_at": iso(at)}
        return {"result": "held", "detail": last.get("needs_person") or ""}
    if update_running(base):
        # A person (or the plugin's bridge) is updating right now; never a second
        # transaction. Recorded, so the next hour tries again (audit A13).
        state["update"] = {"at": iso(at), "result": "another-update-running", "from": config.VERSION,
                           "detail": "another update was running"}
        return {"result": "another-update-running"}
    # A tick (or another workspace operation) in progress is a writer the update would
    # stop half-way: `full update` boots the tick job out before installing. Seen live on
    # 2026-10-06: an automatic update stopped a tick 57 seconds into its run. The daily
    # snapshot already waits for it (`_tick_busy`); the update waits too.
    busy = (["a tick or another workspace operation is running"] if _tick_busy(base) else []) \
        + live_clients(base, at)
    if busy:
        # Activation restarts the tick and the server on the new code: never under a live
        # client or a running tick (LC-16). The release stays `ready`; the next pass tries again.
        state["update"] = {"at": iso(at), "result": "deferred", "from": config.VERSION,
                           "to": check.get("latest"), "detail": "; ".join(busy)}
        update_events.emit("update_restart", "refused", base=base)
        return {"result": "deferred", "detail": "; ".join(busy)}
    args = ["--apply", "--unattended"]
    if _services() is None:
        # No launchd jobs here (Linux): nothing for the update to stop, and saying so is
        # what lets it run at all (audit A02).
        args.append("--writers-stopped")
    state["update_in_flight"] = {"at": iso(at), "to": check.get("latest")}
    write_state(base, state)
    code, report, err = commands.apply(*args, env={"OBSERVATORY_SYSTEM_SETUP": "1"})
    state.pop("update_in_flight", None)
    outcome = {UPDATE_OK: "updated", UPDATE_SERVICES: "updated-services-not-restarted",
               UPDATE_FAILED: "failed-rolled-back", UPDATE_REFUSED: "refused",
               UPDATE_UNDETERMINED: "undetermined", UPDATE_NEEDS_PERSON: "needs-person",
               UPDATE_HELD: "held"}.get(code, "failed")
    update = {"at": iso(at), "exit": code, "result": outcome, "from": config.VERSION,
              "to": report.get("version") or check.get("latest")}
    if code == UPDATE_HELD:
        update["needs_person"] = str(report.get("needs_person") or "")[:1000]
    elif code not in (UPDATE_OK, UPDATE_SERVICES):
        # `full update` names its reason in the JSON it prints, not on stderr.
        update["detail"] = (str(report.get("error") or "") or err or f"exit {code}")[:300]
        update["failures"] = int((state.get("update") or {}).get("failures") or 0) + 1
    else:
        # The check found a release, and it is now installed: the check reads as done, so
        # `maintain status` never says "update available" beside "updated" (audit A14).
        check["result"] = "updated"
    state["update"] = update
    return {"result": outcome}


def _services():
    """This workspace's launchd tick and server, or None where there are none (Linux)."""
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        return None
    import engine_update
    return engine_update.LaunchdServices()


def step_jobs(launch=None) -> dict:
    """The tick and server jobs run on the engine's interpreter (0.19.3).

    Before 0.19.3 their plists could name the installer's interpreter instead, and an
    update restarts them from the files as they are, so an install that had it wrong
    stayed wrong. A plist that names another interpreter is rewritten and reloaded —
    the tick only while it is not running (otherwise the next pass), the server at
    once, which costs its clients a reconnect."""
    if launch is None:
        if sys.platform != "darwin" or shutil.which("launchctl") is None:
            return {"result": "not-applicable"}
        launch = _install_launchd()
    try:
        drift = launch.repair_interpreters(write=False)
        if not drift:
            return {"result": "ok"}
        ready = [j for j in drift if not (j["name"] == "tick" and launch.job_running(j["label"]))]
        if ready:
            launch.repair_interpreters(write=True, only={j["name"] for j in ready})
        fixed = []
        for job in ready:
            if launch.job_loaded(job["label"]):
                launch.stop_job(job["label"])
            ok, detail = launch.start_job(job["plist"])
            fixed.append({"job": job["name"], "was": job["was"], "now": job["now"],
                          "reloaded": ok, **({} if ok else {"detail": str(detail)[:200]})})
        waiting = [j["name"] for j in drift if j not in ready]
        return {"result": "repaired" if fixed else "waiting", "jobs": fixed,
                **({"waiting": waiting} if waiting else {})}
    except Exception as exc:  # noqa: BLE001 — a repair never fails a pass
        return {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def step_snapshot(base: Path, state: dict, at: datetime.datetime, services=None,
                  sleep=time.sleep) -> dict:
    """A full encrypted snapshot once a day. An update's before-upgrade snapshot counts."""
    if not due(state, "snapshot", SNAPSHOT_EVERY, at):
        return {"result": "not-due"}
    import engine_update
    if _tick_busy(base):
        # Booting a running tick out would interrupt it every day (review F10); the next
        # hourly pass tries again.
        return {"result": "deferred", "detail": "a tick or another workspace operation is running"}
    # A person's `full update` stops and starts the same jobs (audit A12). A check was not
    # enough: an update started while the snapshot had the jobs down found nothing to
    # stop, and this step's `finally` then started them in the middle of its install.
    # The snapshot HOLDS the update lock from the stop to the start, so that update is
    # refused instead, and one already running defers the snapshot (A12 review).
    try:
        with engine_update.update_lock(base):
            return _snapshot_with_jobs_stopped(base, state, at, services, sleep)
    except engine_update.UpdateError:
        return {"result": "deferred", "detail": "an update is running"}


def _snapshot_with_jobs_stopped(base: Path, state: dict, at: datetime.datetime, services,
                                sleep) -> dict:
    import workspace_upgrade
    stopped, failures = [], []
    if services is not None:
        for job in services.managed():
            if services.loaded(job["label"]):
                # Recorded BEFORE the stop, so a pass killed in between still leaves the
                # next pass a list of what to start again (review F2).
                state.setdefault("stopped_services", [])
                if job not in state["stopped_services"]:
                    state["stopped_services"].append(job)
                write_state(base, state)
                ok, _detail = services.stop(job["label"])
                if ok:
                    stopped.append(job)
    try:
        last = ""
        for attempt in range(SNAPSHOT_ATTEMPTS):
            try:
                result = workspace_upgrade.snapshot(base, writers_stopped=True, kind=SNAPSHOT_KIND)
                record = {"at": iso(at), "result": "taken", "encrypted": bool(result.get("encrypted")),
                          "file": Path(result["snapshot"]).name}
                if result.get("export_error"):
                    record["detail"] = result["export_error"][:300]
                state["snapshot"] = record
                state.pop("snapshot_failure", None)
                return {"result": "taken", "encrypted": record["encrypted"]}
            except (config.ConfigurationError, sqlite3.Error, OSError) as exc:
                # A session MCP server's journal line, a tick still releasing its lock, or a
                # database copy torn by a session server writing during it (seen live on
                # 2026-10-05: "database disk image is malformed" on the COPY; the live store
                # was intact). The copy is verified, so a retry is safe.
                last = f"{type(exc).__name__}: {exc}"
                sleep(10 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 — a pass records a failure, it never crashes
                last = f"{type(exc).__name__}: {exc}"
                break
        state["snapshot_failure"] = {"at": iso(at), "detail": last[:300]}
        return {"result": "failed", "detail": last[:300]}
    finally:
        for job in stopped:
            ok, detail = services.start(job["plist"])
            if not ok:
                failures.append({"service": job["name"], "detail": detail[:300]})
        state.pop("stopped_services", None)
        if failures:
            state["services_not_restarted"] = failures
        elif stopped:
            state.pop("services_not_restarted", None)


def _tick_busy(base: Path) -> bool:
    """Whether a tick or another workspace operation holds its lock right now."""
    import workspace_upgrade
    try:
        with workspace_upgrade.operation_lock(base):
            return False
    except config.ConfigurationError:
        return True


def restart_left_stopped(base: Path, state: dict, services) -> list[dict]:
    """Start what an interrupted pass stopped and never started again (review F2)."""
    left = state.pop("stopped_services", None) or []
    if not left or services is None:
        return []
    started = []
    for job in left:
        if not services.loaded(job["label"]):
            ok, detail = services.start(job["plist"])
            started.append({"service": job["name"], "started": ok, "detail": "" if ok else detail[:300]})
    return started


def run_pass(base: Path, *, at: datetime.datetime | None = None, commands: Commands | None = None,
             services=..., store=None, app=None, sleep=time.sleep, first_check_delay: float = 0,
             clock=time.monotonic) -> dict:
    """One pass of everything that is due. Never raises for a step's own failure: each
    is recorded in store/maintenance.json and named in the answer.

    `first_check_delay` (the scheduler's `--first-check-delay`): a check this pass makes
    waits until that many seconds after the process started, so a job loaded at login does
    not reach the network in the same moment as everything else (LC-16)."""
    at = at or now()
    config.validate_workspace(base, required=True)
    with pass_lock(base) as held:
        if not held:
            return {"status": "busy", "detail": "another maintenance pass holds the lock"}
        state = read_state(base)
        report: dict = {"status": "ran", "at": iso(at)}
        services = _services() if services is ... else services
        recovered = restart_left_stopped(base, state, services)
        if recovered:
            report["restarted_after_interruption"] = recovered
        try:
            report["passphrase"] = backup_vault.ensure_passphrase(base, store)
        except (backup_vault.BackupError, OSError) as exc:
            report["passphrase"] = {"error": str(exc)[:300]}
        # Where the passphrase is kept is part of what doctor and Health show (audit A14):
        # recorded by name and place, never the value.
        state["passphrase"] = {k: v for k, v in report["passphrase"].items()
                               if k in ("passphrase", "kept_outside", "warning", "error")}
        def settle() -> None:
            wait = first_check_delay - (clock() - PROCESS_STARTED)
            if wait > 0:
                sleep(wait)
        report["update"] = step_update(base, state, at, commands or Commands(base),
                                       before_check=settle if first_check_delay > 0 else None)
        if report["update"]["result"].startswith("updated"):
            # The new release's code is what should run next: the app and the snapshot
            # wait for the next pass, and the update's before-upgrade snapshot counts.
            state["snapshot"] = {"at": iso(at), "result": "before-upgrade"}
            report["next"] = "the next pass runs the new release"
        else:
            if not auto_enabled(base):
                # The switch covers the app too: off means no check, download or install.
                report["app"] = {"result": "off"}
            elif sys.platform == "darwin" or app is not None:
                try:
                    import app_update
                    report["app"] = (app or app_update.AppUpdater(base)).step(state, at)
                except Exception as exc:  # noqa: BLE001 — the app never stops a backup
                    report["app"] = {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
            report["snapshot"] = step_snapshot(base, state, at, services, sleep=sleep)
            if services is not None:
                report["jobs"] = step_jobs()
        state["pass"] = {"at": iso(at)}
        write_state(base, state)
        _rotate_logs(base)
        return report


def run_app_step(base: Path, *, at: datetime.datetime | None = None, app=None) -> dict:
    """Only the app step of a pass, under the same lock and in the same record.

    The Mac app starts this from a detached helper once it has quit — its *Restart to
    update*, a quit, or a long idle — so a verified bundle that waited with
    `waiting-for-quit` is swapped in now instead of at the next hourly pass. Nothing else
    a pass does (the engine update, the backup, the passphrase) runs here."""
    at = at or now()
    config.validate_workspace(base, required=True)
    if sys.platform != "darwin" and app is None:
        return {"status": "skipped", "detail": "the app exists only on macOS"}
    with pass_lock(base) as held:
        if not held:
            return {"status": "busy", "detail": "another maintenance pass holds the lock"}
        state = read_state(base)
        try:
            import app_update
            result = (app or app_update.AppUpdater(base)).step(state, at)
        except Exception as exc:  # noqa: BLE001 — recorded, as in a full pass
            result = {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        write_state(base, state)
        return {"status": "ran", "at": iso(at), "app": result}


def _rotate_logs(base: Path) -> None:
    """The job's own logs and the plugin hook's, under the engine's log policy (LC-12).
    The tick rotates store/logs too, but the tick is off by default (audit A19)."""
    try:
        import log_policy
        log_policy.sweep(base / "store" / "logs")
        log_policy.sweep(update_events.directory())
        state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
        hooks = state_dir / "project-observatory"
        if hooks.is_dir():
            log_policy.sweep(hooks)
    except Exception:  # noqa: BLE001 — rotation never fails a pass
        pass


# --- the schedule ------------------------------------------------------------------

def _install_launchd():
    sys.path.insert(0, str(config.SOURCE / "tools"))
    import install_launchd
    return install_launchd


def _instance_digest(base: Path) -> str:
    return hashlib.sha256(str(base.resolve()).encode()).hexdigest()[:16]


class LaunchdSchedule:
    """org.project-observatory.<sha16>.maintain: hourly, at login too, never a managed job."""

    kind = "launchd"

    def __init__(self, base: Path, runner=None):
        self.base = base
        self.launch = _install_launchd()
        self.label = f"org.project-observatory.{_instance_digest(base)}.maintain"
        self.plist = Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"
        self.runner = runner

    def document(self) -> dict:
        env = self.launch.environment()
        # PATH comes from whoever runs the installer, and a terminal and an agent session
        # differ: an installed job keeps the PATH it has, so the definition does not flip
        # (and get reloaded) with each caller (review F1).
        if self.installed():
            try:
                kept = (plistlib.loads(self.plist.read_bytes()).get("EnvironmentVariables") or {}).get("PATH")
            except (OSError, ValueError, plistlib.InvalidFileException):
                kept = None
            if kept:
                env["PATH"] = kept
        env["OBSERVATORY_HOME"] = str(self.base)
        env["OBSERVATORY_PYTHON"] = self.launch.stable_interpreter(engine_python())
        env["OBSERVATORY_SYSTEM_SETUP"] = "1"
        logs = self.base / "store" / "logs"
        return {
            "Label": self.label,
            # RunAtLoad starts the pass at login with everything else; its check waits
            # FIRST_CHECK_DELAY seconds after the start (LC-16).
            "ProgramArguments": [self.launch.stable_interpreter(engine_python()),
                                 str(config.SOURCE / "tools" / "maintain.py"), "run",
                                 "--first-check-delay", str(FIRST_CHECK_DELAY)],
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": True,
            "WorkingDirectory": str(config.SOURCE),
            "StandardOutPath": str(logs / "maintain.log"),
            "StandardErrorPath": str(logs / "maintain.err"),
            # Not "Background" with LowPriorityIO: on a loaded machine that throttling
            # stretched a 2-second `full update --check` past its 600-second limit (seen
            # live, 2026-10-05). The pass is short and hourly; a nice value is enough.
            "ProcessType": "Standard",
            "Nice": 10,
            "EnvironmentVariables": env,
            "Umask": 0o077,
        }

    def installed(self) -> bool:
        return self.plist.is_file()

    def loaded(self) -> bool:
        return self.launch.job_loaded(self.label)

    def install(self) -> dict:
        doc = self.document()
        problems = self.launch.lint_plist(doc)
        if problems:
            raise config.ConfigurationError("; ".join(problems))
        self.launch.prepare_logs(("maintain.log", "maintain.err"), self.base / "store" / "logs")
        current = plistlib.loads(self.plist.read_bytes()) if self.installed() else None
        pending = self.base / "store" / "maintenance-reload-pending"
        if current == doc and self.loaded() and not pending.exists():
            return {"schedule": self.kind, "label": self.label, "changed": False}
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        if self.plist.is_symlink():
            raise config.ConfigurationError("LaunchAgent plist cannot be a symbolic link")
        tmp = self.plist.with_suffix(".plist.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(plistlib.dumps(doc))
        os.replace(tmp, self.plist)
        if self.loaded():
            if pass_running(self.base):
                # Booting the job out now would kill the pass that is running — perhaps the
                # very `full update` asking for this schedule. The new definition is on disk;
                # the next ensure outside a pass (the plugin hook, a person) loads it.
                pending.write_text(iso(now()) + "\n")
                return {"schedule": self.kind, "label": self.label, "changed": True, "reload": "deferred"}
            self.launch.stop_job(self.label)
        ok, detail = self.launch.start_job(str(self.plist))
        if not ok:
            raise config.ConfigurationError(f"launchd did not load {self.label}: {detail[:200]}")
        pending.unlink(missing_ok=True)
        return {"schedule": self.kind, "label": self.label, "changed": True}

    def uninstall(self) -> dict:
        if self.loaded():
            self.launch.stop_job(self.label)
        self.plist.unlink(missing_ok=True)
        return {"schedule": self.kind, "label": self.label, "removed": True}

    def status(self) -> dict:
        return {"kind": self.kind, "label": self.label, "installed": self.installed(), "loaded": self.loaded()}


class SystemdSchedule:
    """A systemd user timer: hourly, 90 seconds after the user manager starts (LC-16)."""

    kind = "systemd"

    def __init__(self, base: Path, runner=subprocess.run, which=shutil.which):
        self.base, self.runner, self.which = base, runner, which
        self.name = f"project-observatory-{_instance_digest(base)}-maintain"
        root = os.environ.get("XDG_CONFIG_HOME")
        self.units = (Path(root) if root and Path(root).is_absolute() else Path.home() / ".config") / "systemd" / "user"

    @staticmethod
    def quote(value: str) -> str:
        # `%` is a systemd specifier and `$` expands in ExecStart: a path holding either
        # would run something else than it names, so it is refused rather than escaped.
        if any(c in value for c in '"\\\n%$'):
            raise config.ConfigurationError("A path with a quote, backslash, newline, % or $ cannot go in a systemd unit")
        return f'"{value}"'

    def service_text(self) -> str:
        python = _install_launchd().stable_interpreter(engine_python())
        return "\n".join([
            "[Unit]",
            f"Description=Project Observatory maintenance (updates and backups) for {self.base}",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={self.quote(python)} {self.quote(str(config.SOURCE / 'tools' / 'maintain.py'))} run",
            f"Environment={self.quote('OBSERVATORY_HOME=' + str(self.base))}",
            'Environment="OBSERVATORY_SYSTEM_SETUP=1"',
            "Nice=10",
            "UMask=0077",
            # The pass starts an update in a session of its own and never waits to kill
            # it (audit A03); systemd's default control-group kill would take it down with
            # the pass on a stop or a logout, leaving the jobs it stopped down (A03 review).
            "KillMode=process",
            "",
        ])

    def timer_text(self) -> str:
        return "\n".join([
            "[Unit]",
            f"Description=Run Project Observatory maintenance hourly for {self.base}",
            "",
            "[Timer]",
            # The user manager starts at login: the first pass, and so the first check,
            # comes 90 seconds after it (LC-16), then hourly.
            f"OnStartupSec={FIRST_CHECK_DELAY}s",
            f"OnUnitActiveSec={INTERVAL_SECONDS // 60}min",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])

    def _systemctl(self, *args: str) -> tuple[int, str]:
        try:
            p = self.runner(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, type(exc).__name__
        return p.returncode, (p.stdout + p.stderr).strip()

    def available(self) -> bool:
        return self.which("systemctl") is not None

    def installed(self) -> bool:
        return (self.units / f"{self.name}.timer").is_file()

    def install(self) -> dict:
        if not self.available():
            raise config.ConfigurationError("no systemd user manager (`systemctl --user`) on this machine")
        self.units.mkdir(parents=True, exist_ok=True)
        changed = False
        for suffix, text in ((".service", self.service_text()), (".timer", self.timer_text())):
            file = self.units / f"{self.name}{suffix}"
            if not file.is_file() or file.read_text() != text:
                tmp = file.with_suffix(suffix + ".tmp")
                tmp.write_text(text)
                os.replace(tmp, file)
                changed = True
        for args in (("daemon-reload",), ("enable", "--now", f"{self.name}.timer")):
            code, out = self._systemctl(*args)
            if code != 0:
                raise config.ConfigurationError(f"systemctl --user {' '.join(args)} failed: {out[:200]}")
        return {"schedule": self.kind, "unit": f"{self.name}.timer", "changed": changed}

    def uninstall(self) -> dict:
        if self.available():
            self._systemctl("disable", "--now", f"{self.name}.timer")
        for suffix in (".service", ".timer"):
            (self.units / f"{self.name}{suffix}").unlink(missing_ok=True)
        if self.available():
            self._systemctl("daemon-reload")
        return {"schedule": self.kind, "unit": f"{self.name}.timer", "removed": True}

    def status(self) -> dict:
        active = None
        if self.available() and self.installed():
            active = self._systemctl("is-active", f"{self.name}.timer")[0] == 0
        return {"kind": self.kind, "unit": f"{self.name}.timer", "installed": self.installed(), "active": active}


def schedule_for(base: Path):
    return LaunchdSchedule(base) if sys.platform == "darwin" else SystemdSchedule(base)


def ensure(base: Path, *, schedule=None, store=None, explicit: bool = False) -> dict:
    """Schedule the pass and keep the passphrase outside the workspace (D2, D5).

    Idempotent and quick: a schedule already in place is left as it is. A person's
    `maintain uninstall` is respected (`updates.scheduled` false) unless this call is the
    person's own `maintain ensure` or `auto-update on` (`explicit`)."""
    config.validate_workspace(base, required=True)
    if explicit and not schedule_wanted(base):
        set_setting(base, "scheduled", True)
    out: dict = {"auto_update": auto_enabled(base)}
    try:
        out["passphrase"] = backup_vault.ensure_passphrase(base, store)
    except (backup_vault.BackupError, OSError) as exc:
        out["passphrase"] = {"error": str(exc)[:300]}
    if not schedule_wanted(base):
        out["schedule"] = {"result": "off", "detail": "turned off with `maintain uninstall`; "
                                                     "`project-observatory full maintain ensure` turns it on"}
        return out
    schedule = schedule or schedule_for(base)
    try:
        out["schedule"] = schedule.install()
    except (config.ConfigurationError, OSError) as exc:
        out["schedule"] = {"result": "unscheduled", "detail": str(exc)[:300],
                           "fallback": "the observatory-log plugin runs the pass at each new session"}
    return out


def unschedule(base: Path, *, schedule=None) -> dict:
    config.validate_workspace(base, required=True)
    if pass_running(base):
        # Booting the job out now would stop the pass and perhaps an update it runs
        # (audit A03).
        raise config.ConfigurationError("a maintenance pass is running; run `maintain uninstall` "
                                        "again when it has ended (`maintain status` shows the last pass)")
    set_setting(base, "scheduled", False)
    return (schedule or schedule_for(base)).uninstall()


# --- what doctor and the Health page read -------------------------------------------

def status(base: Path, *, schedule=None) -> dict:
    """Whether updates and backups run by themselves, and what they last found. No value."""
    state = read_state(base)
    if schedule is None and not probes_allowed():
        sched = {"probed": False, "detail": "OBSERVATORY_SYSTEM_SETUP=0: launchd/systemd not asked"}
    else:
        try:
            sched = (schedule or schedule_for(base)).status()
        except Exception as exc:  # noqa: BLE001 — a status never fails doctor
            sched = {"error": f"{type(exc).__name__}"}
    warnings, items = [], []

    def warn(code: str, text: str, **params) -> None:
        # The sentence for `maintain status` and doctor, and a code with its parameters
        # for the Health page, which shows each in the reader's language and leaves out
        # what one of its own rows already says (A14 review: every problem twice, the
        # second time in English).
        warnings.append(text)
        items.append({"code": code, "text": text, **params})

    # Scheduled means installed AND loaded (or, on Linux, the timer active) — audit A14.
    scheduled = bool(sched.get("installed")) and sched.get("loaded", sched.get("active")) is True
    if not schedule_wanted(base):
        warn("schedule-off", "the maintenance schedule was turned off; updates and daily backups do not run by "
                             "themselves: `project-observatory full maintain ensure`")
    elif sched.get("probed") is False:
        pass
    elif not scheduled:
        warn("not-scheduled", "updates and daily backups are not scheduled on this machine yet: "
                              "`project-observatory full maintain ensure`")
    if not auto_enabled(base):
        warn("auto-off", f"{SWITCH_LABEL}: off — new releases wait for you: `project-observatory full auto-update on`")
    update = state.get("update") or {}
    if state.get("services_not_restarted") or update.get("result") == "updated-services-not-restarted":
        # This engine's own interpreter, as the dashboard's commands carry it: a Mac has no
        # `python` on PATH (A29), and a command that cannot run is not a remedy.
        py = shlex.quote(engine_python())
        warn("services-not-restarted",
             "a background job did not start again after the last backup or update: "
             f"`{py} \"$(project-observatory full-path)/tools/serverd.py\" --install` starts the "
             f"server, `{py} \"$(project-observatory full-path)/tools/install_launchd.py\" install` "
             "the tick (with `features.scheduler` on)")
    if (state.get("app") or {}).get("result") == "refused":
        detail = str((state.get("app") or {}).get("detail", ""))[:240]
        warn("app-refused", f"the Mac app update was refused: {detail}", detail=detail)
    if update.get("result") == "held" and not _engine_moved_past(update.get("to")):
        step = str(update.get("needs_person") or "")[:400]
        warn("needs-migration", f"release {update.get('to')} needs a person before it is installed: {step}; "
                                "then `project-observatory full update --apply`",
             version=update.get("to"), step=step)
    if update.get("result") == "deferred":
        warn("deferred", f"release {update.get('to')} is ready and waits for a moment with no client "
                         f"({update.get('detail')}); it is tried again the next hour",
             version=update.get("to"), detail=str(update.get("detail") or ""))
    if update.get("result") == "needs-person":
        warn("needs-person", "the last automatic update needs a person: run `project-observatory full update`")
    elif update.get("failures"):
        hour = _transient(update)
        detail = str(update.get("detail", ""))[:120]
        warn("update-incomplete", f"the last automatic update did not complete ({update.get('result')}: "
                                  f"{detail}); it is tried again {'the next hour' if hour else 'within six hours'}",
             result=update.get("result"), detail=detail, soon=hour)
    failure = state.get("snapshot_failure") or {}
    if failure and (parse_iso(failure.get("at")) or now()) >= (parse_iso((state.get("snapshot") or {}).get("at"))
                                                              or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)):
        detail = str(failure.get("detail", ""))[:160]
        warn("snapshot-failed", f"the last daily backup failed: {detail}; it is tried again the next hour",
             detail=detail)
    phrase = state.get("passphrase") or {}
    if phrase.get("warning") or phrase.get("error"):
        warn("passphrase", str(phrase.get("warning") or phrase.get("error"))[:200])
    snapshot = state.get("snapshot") or {}
    if snapshot.get("result") == "taken" and snapshot.get("encrypted") is False:
        warn("not-encrypted", "the last daily backup is not encrypted and stays inside the workspace: "
                              + (snapshot.get("detail") or "no passphrase"))
    snap = parse_iso(snapshot.get("at"))
    if snap is None or now() - snap > SNAPSHOT_EVERY * 2:
        warn("no-backup", "no full backup in the last two days")
    versions = {"engine": config.VERSION}
    if sys.platform == "darwin":
        try:
            import app_update
            app = app_update.installed_app()
            if app is not None:
                versions["app"] = (app_update.bundle_info(app) or {}).get("version")
        except Exception:  # noqa: BLE001
            pass
    return {"auto_update": auto_enabled(base), "switch": switch_status(base),
            "schedule_wanted": schedule_wanted(base), "schedule": sched,
            "scheduled": scheduled, "versions": versions, "passphrase": state.get("passphrase"),
            "last_pass": (state.get("pass") or {}).get("at"), "check": state.get("check"),
            "update": state.get("update"), "update_in_flight": state.get("update_in_flight"),
            "snapshot": state.get("snapshot"), "snapshot_failure": state.get("snapshot_failure"),
            "app": state.get("app"), "warnings": warnings, "warning_items": items}


# --- a reinstall restores ------------------------------------------------------------

def restore_latest(base: Path, *, store=None, label: str | None = None) -> dict:
    """Restore the newest backup a workspace at this path left (D7). Only into an empty home.

    When backups of more than one workspace exist under this name (a reinstall that could
    not find its passphrase starts a second, empty workspace with its own daily backups),
    nothing is chosen: the answer lists them and `--label` picks one (audit A06). Within
    one workspace the newest snapshot that opens is restored, falling back to older ones."""
    import workspace_upgrade
    if base.exists() and any(p.name != ".workspace.lock" for p in base.iterdir()):
        raise config.ConfigurationError("Restore requires a new empty destination; existing state is never overwritten")
    found = backup_vault.candidates(base)
    if label:
        found = [c for c in found if c["label"] == label]
        if not found:
            return {"status": "no-backup", "detail": f"no backups under the label {label!r}"}
    if not found:
        return {"status": "no-backup", "detail": f"no backups of a workspace named {base.name!r} were found"}
    labels: dict[str, dict] = {}
    for c in found:  # newest first, so the first one seen per label is its newest
        entry = labels.setdefault(c["label"], {"label": c["label"], "newest": str(c["snapshot"]),
                                                "stamp": c["stamp"], "snapshots": 0})
        entry["snapshots"] += 1
    if len(labels) > 1:
        return {"status": "several-workspaces", "backups": list(labels.values()),
                "detail": "backups of more than one workspace with this name exist; choose one",
                "next": "project-observatory full restore --latest --label LABEL"}
    # OBSERVATORY_SYSTEM_SETUP=0 keeps this process away from the Keychain (audit A25);
    # a store handed in by the caller is its own choice and is used.
    env_secret = os.environ.get(backup_vault.PASS_ENV)
    if store is None and os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0":
        secrets_for = lambda _label: [env_secret] if env_secret else []  # noqa: E731
    else:
        store = store or backup_vault.SecretStore()
        secrets_for = lambda lbl: ([env_secret] if env_secret else []) + backup_vault.stored_passphrases(store, lbl)  # noqa: E731
    problems: list[dict] = []
    tried_any = False
    for candidate in found:
        for secret in secrets_for(candidate["label"]):
            tried_any = True
            try:
                result = workspace_upgrade.restore(candidate["snapshot"], base, secret=secret)
            except (backup_vault.BackupError, config.ConfigurationError, OSError) as exc:
                # A wrong passphrase, a damaged copy or one that needs a newer release: try
                # the next passphrase, then the next older backup (review F10).
                problems.append({"snapshot": candidate["snapshot"].name, "detail": str(exc)[:200]})
                if not base.exists() or not any(p.name != ".workspace.lock" for p in base.iterdir()):
                    continue
                raise
            result.update({"status": "restored", "from": str(candidate["snapshot"]), "label": candidate["label"]})
            if problems:
                result["skipped"] = problems
            return result
    newest = found[0]
    # The command a person can run as given: an EMPTY home (a fresh `init` already made
    # this one non-empty), and no passphrase on the command line — `restore` asks for it
    # at a terminal (audit A05).
    by_hand = f"project-observatory --home NEW_EMPTY_HOME full restore '{newest['snapshot']}'"
    if tried_any and not all("authentication" in p["detail"] for p in problems):
        return {"status": "restore-failed", "newest": str(newest["snapshot"]), "skipped": problems,
                "next": "`project-observatory full init --fresh` starts empty; the backups are left as they are"}
    return {"status": "backups-found-locked", "newest": str(newest["snapshot"]),
            "detail": "no passphrase that opens these backups was found in the OS credential store",
            "next": by_hand + " (it asks for the passphrase)"}


# --- CLI ------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project-observatory full maintain",
                                 description="updates that arrive by themselves and a daily backup")
    ap.add_argument("action", choices=["run", "ensure", "uninstall", "status", "hook", "app"])
    ap.add_argument("--if-wanted", action="store_true",
                    help="with ensure: keep a schedule a person removed with `maintain uninstall` removed")
    ap.add_argument("--first-check-delay", type=float, default=0, metavar="SECONDS",
                    help="with run: a check waits until SECONDS after this process started (the scheduler's)")
    return ap


def hook(base: Path) -> dict:
    """What the observatory-log plugin runs, detached, at a session's start.

    It schedules the job where nothing is scheduled yet (respecting `maintain uninstall`),
    and where no scheduler exists — Linux without a systemd user manager — it runs the
    pass itself, which is cheap when nothing is due."""
    result = ensure(base)
    sched = result.get("schedule") or {}
    if sched.get("result") == "unscheduled":
        result["pass"] = run_pass(base)
    return result


def auto_update_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project-observatory full auto-update",
                                 description=f"{SWITCH_LABEL} (on by default): the file `auto-update` "
                                             "in the workspace home")
    ap.add_argument("action", choices=["status", "on", "off"])
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    try:
        base = config.home()
        # `run` and `hook` write the Keychain and may bootstrap launchd: the same consent
        # `ensure` asks for (audit A07 review). The jobs and the plugin hook set
        # OBSERVATORY_SYSTEM_SETUP=1; a person's terminal passes on its own.
        if a.action in ("run", "hook") and not system_setup_allowed(base):
            raise config.ConfigurationError(
                f"`maintain {a.action}` touches launchd/systemd and the credential store; this process "
                "may not (OBSERVATORY_SYSTEM_SETUP=0, a temporary workspace, or not this account's home). "
                "Run it from a terminal, or with OBSERVATORY_SYSTEM_SETUP=1")
        if a.action == "run":
            # launchd and systemd stop a job with SIGTERM; the default handler would skip
            # the `finally` that starts the tick and server again (review F2).
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
            result = run_pass(base, first_check_delay=max(0.0, min(a.first_check_delay, 600.0)))
        elif a.action == "ensure":
            if not system_setup_allowed(base):
                raise config.ConfigurationError(
                    "scheduling touches launchd/systemd and the credential store: run it from a terminal, "
                    "or with OBSERVATORY_SYSTEM_SETUP=1")
            result = ensure(base, explicit=not a.if_wanted)
        elif a.action == "uninstall":
            result = unschedule(base)
        elif a.action == "hook":
            result = hook(base)
        elif a.action == "app":
            result = run_app_step(base)
        else:
            result = status(base)
        print(json.dumps(result, indent=2))
        return 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2


def auto_update_main(argv: list[str]) -> int:
    a = auto_update_parser().parse_args(argv)
    try:
        base = config.home()
        config.validate_workspace(base, required=True)
        migrated = migrate_legacy(base)
        if a.action in ("on", "off"):
            write_switch(base, a.action == "on")
            update_events.emit("auto_update", a.action, base=base)
        result = {"auto_update": auto_enabled(base), "switch": switch_status(base)}
        if a.action == "on" and system_setup_allowed(base):
            result["ensure"] = ensure(base, explicit=True)
        elif a.action == "status":
            result = status(base)
        if migrated:
            result["migrated"] = migrated
            print(f"Observatory: {migrated}", file=sys.stderr)
        print(json.dumps(result, indent=2))
        return 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
