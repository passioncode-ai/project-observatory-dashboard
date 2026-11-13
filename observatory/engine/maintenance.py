"""Updates that arrive by themselves, and a daily backup that leaves the workspace.

The decisions behind it are in docs/runs/2026-10-05-auto-update (D1–D8). One job per
workspace runs `tools/maintain.py run` every 15 minutes — launchd on macOS, a systemd user
timer on Linux — and each pass does only what is due:

1. a backup passphrase exists and is kept outside the workspace (backup_vault.SecretStore);
2. every five minutes (operator rule, 2026-10-17), with automatic updates on (the default;
   the switch is below):
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
   store), with this workspace's tick and server stopped for the copy;
5. once a day: the machine's agent setup into one encrypted `.obmachine` bundle in the same
   root (machine_backup.py, OBS-157), with every job running;
6. once an hour, where this node holds a share with another person's machine (OBS-264):
   `full share sync --due`, in a process of its own (`step_shares`): the sender reads reports,
   seals again before a sealed copy ends or after a rotation, and hands the mailbox a revocation
   it did not take; the recipient renews and reports at least daily. A failed pass is recorded as
   `shares.unsynced` and retried at the next hour.

The scheduler runs the pass every 15 minutes and at load; launchd passes `--first-check-delay 90`
so the first check waits 90 seconds after the job starts (a login, a boot), and the
systemd timer starts 90 seconds after the user manager does (lifecycle LC-16).

THE CHECK JOB. "Is there something newer" is asked every five minutes, the retry after a check
that could not look included (operator rule, 2026-10-17), by a SECOND job of the same workspace,
`maintain.py check` (launchd `.check`, systemd `-check`, Task Scheduler `check`): one read of the
newest release's static SHA256SUMS on github.com, in this process (`engine_update.latest_version`),
never the REST API's 60 requests an hour and never the pass above, whose guard, backup and
update may take longer than five minutes (LC-03: a job never runs past its own interval). When it
finds a newer release it starts the pass at once (`kick`), unless that release is held for a
person or was tried in the last 15 minutes; the pass applies it at a safe point as before. A check
that finds what the last one found prints nothing and logs nothing new (`update_events`). With
automatic updates off the job still reads the feed on the same schedule (OBS-333, ADR-0188 item 4)
but never starts the pass: a newer version is recorded as `available` in `update-check.json` and
announced once per version (the job's output, `full doctor`, `full auto-update status`, Home and
Health in the dashboard, the Mac app's menu), with the command that installs it,
`full update --apply`. Where no scheduler exists the plugin hook runs this check while off.

THE SWITCH (LC-16) is the file `auto-update` in the workspace home: absent means on, and
only the word `off` turns automatic updates off. `full auto-update on|off` writes it; an
update, a reinstall or an uninstall never does. Installs that turned updates off before
0.19.0 did it with `updates.auto: false` in config/settings.json: that is still honoured
as off while the file is absent, and the next `auto-update` command moves it into the file
and says so. Off stops the downloads and the installs, the app's included, and never starts
the pass for a release; the check keeps reading the feed so a newer version is still announced
(OBS-333), and the daily backup keeps running. An update never turns a person's off back on.

    full auto-update status|on|off     the switch, and a newer version waiting while it is off
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
import oslocks
import osproc
import osprivacy
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
#: How often a newer release is looked for: every five minutes (operator rule, 2026-10-17,
#: relayed to every PassionCode product; it was six hours under LC-16).
CHECK_EVERY = datetime.timedelta(minutes=5)
#: A check that could not look (network, a timeout on a loaded machine) is tried again on the
#: same five-minute cadence: the rule names the retry too.
RETRY_UNDETERMINED = CHECK_EVERY
#: The check job's own interval, in seconds (CHECK_EVERY), and the most it may run (LC-03).
CHECK_INTERVAL_SECONDS = 300
CHECK_TIME_LIMIT = 240
#: The check job's first run after a login, offset from the pass's FIRST_CHECK_DELAY so the two
#: jobs never start together (review H1); the pass's own check covers the first 90 seconds.
CHECK_FIRST_DELAY = 150
#: An update that failed for a lasting reason (rolled back, a refused verification) is not tried
#: again for the same release before this: the check every five minutes keeps finding it, and each
#: attempt stops the tick and server, snapshots and installs. A momentary refusal or a deferral
#: is tried at the next pass (`_transient`).
RETRY_FAILED_UPDATE = datetime.timedelta(hours=6)
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
    if record.get("result") == "refused-disk":
        # Refused before it began for too little free space (exit 7): the disk guard runs on
        # every pass and a person may free space, so the next hour may fit.
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
#: How long a failed snapshot waits before it is tried again (it stops the tick and server).
SNAPSHOT_RETRY = datetime.timedelta(hours=1)
#: Every 15 minutes (2026-10-11; it was an hour). Everything else in a pass is gated by its own
#: cadence (the update check every 5 minutes, the backup daily), so a pass that is not due for
#: them is quick; the disk guard is what needs the frequency — below the `low` line it runs
#: on every pass, because a full disk had stopped the machine inside one hourly gap.
INTERVAL_SECONDS = 900
#: Exit codes of `full update` (engine_update.EXIT_*), named here so a change there is
#: caught by tests/test_maintenance.py rather than by a user's stalled install.
UPDATE_OK, UPDATE_FAILED, UPDATE_REFUSED, UPDATE_UNDETERMINED = 0, 1, 2, 3
UPDATE_NEEDS_PERSON, UPDATE_SERVICES, UPDATE_HELD, UPDATE_NO_SPACE, UPDATE_AVAILABLE = 4, 5, 6, 7, 10
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


#: The check job's own record (`check_pass`). Never maintenance.json: the job holds no pass lock, so
#: a read-modify-write of the pass's record from beside a running pass would undo what the pass wrote.
CHECK_FILE = "update-check.json"


def check_path(base: Path) -> Path:
    return base / "store" / CHECK_FILE


def read_check(base: Path) -> dict:
    return read_state(base, check_path(base))


def write_check(base: Path, doc: dict) -> None:
    write_state(base, doc, check_path(base))


def read_state(base: Path, file: Path | None = None) -> dict:
    file = file or state_path(base)
    if not file.is_file() or file.is_symlink():
        return {}
    try:
        doc = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state(base: Path, doc: dict, file: Path | None = None) -> None:
    file = file or state_path(base)
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


def due(state: dict, key: str, every: datetime.timedelta, at: datetime.datetime,
        slack: datetime.timedelta = datetime.timedelta(0)) -> bool:
    """Whether `every` has passed since `state[key].at`. `slack` absorbs a scheduler's start jitter
    when `every` equals its interval: without it a run 0.2 s early is not due, and a five-minute
    cadence becomes ten (review M1)."""
    last = parse_iso((state.get(key) or {}).get("at"))
    return last is None or at - last >= every - slack


#: A scheduler starts a job a little early or late; a check this close to its five minutes is due.
CHECK_SLACK = datetime.timedelta(seconds=30)


def last_check(state: dict, record: dict) -> dict:
    """The newer of the pass's own check and the check job's (`update-check.json`)."""
    mine, job = state.get("check") or {}, record.get("check") or {}
    a, b = parse_iso(mine.get("at")), parse_iso(job.get("at"))
    if b is not None and (a is None or b > a):
        return {**job, "source": "check-job"}
    return mine


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
    return sys.stdin is not None and osprivacy.is_terminal(sys.stdin)


#: Where the operating system keeps temporary directories, whatever TMPDIR this process was
#: handed. A test runner gives each child its own TMPDIR beside the workspace it builds
#: (`base/tmp` next to `base/runtime`), so the child's `gettempdir()` is not an ancestor of
#: that workspace — but the runner's own temporary directory is, and on macOS that lives
#: under /var/folders (audit A24: the guard's test patched `gettempdir` in-process and could
#: not see this). No real workspace lives under any of these.
SYSTEM_TEMP_ROOTS = (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"), Path("/private/var/tmp"),
                     Path("/var/folders"), Path("/private/var/folders"))


def _real_home() -> bool:
    try:
        real = osprivacy.real_home()
        return real is not None and Path.home().resolve() == real.resolve()
    except (OSError, RuntimeError):
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
                    cwd=tempfile.gettempdir(), **osproc.new_group())
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
    fd = osprivacy.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | osprivacy.NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


# --- one pass ---------------------------------------------------------------------

@contextlib.contextmanager
def pass_lock(base: Path):
    """One pass at a time per workspace; a second one finds it busy and leaves."""
    file = base / "store" / "maintenance.lock"
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(file, os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
    try:
        try:
            oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
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
    fd = osprivacy.open(file, os.O_RDWR | osprivacy.NOFOLLOW)
    try:
        oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
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
    receipt = base / "store" / "raw" / "serverd.json"  # paths-check: allow — the receipt of the workspace `base` passed in, which need not be this process's paths.HOME; a test redirects it through `base`
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


def _retry_after(last: dict, latest) -> datetime.datetime | None:
    """When a release whose automatic update failed for a lasting reason may be tried again."""
    if not last.get("failures") or last.get("to") != latest or _transient(last):
        return None
    failed = parse_iso(last.get("at"))
    return failed + RETRY_FAILED_UPDATE if failed else None


def _newer(target) -> bool:
    """Whether `target` is a release newer than the running engine."""
    try:
        return config.version_tuple(str(target)) > config.version_tuple(config.VERSION)
    except (ValueError, TypeError, config.ConfigurationError):
        return False


def _engine_moved_past(target) -> bool:
    try:
        return config.version_tuple(config.VERSION) >= config.version_tuple(str(target))
    except (ValueError, TypeError, config.ConfigurationError):
        return False


def step_update(base: Path, state: dict, at: datetime.datetime, commands: Commands,
                before_check=None) -> dict:
    """Check every five minutes; apply a newer stable release at a safe point. Returns what happened."""
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
    if (str(last.get("result") or "").startswith("updated") and last.get("to")
            and last.get("to") != config.VERSION and _engine_moved_past(last.get("to"))):
        # A person's `full update` moved the engine past the last automatic one; the record
        # said "updated to <that one>" beside the newer engine (0.20.1).
        state["update"] = last = {"at": iso(at), "result": "updated-by-person",
                                  "from": last.get("to"), "to": config.VERSION}
    if last.get("result") == "held" and _engine_moved_past(last.get("to")):
        # The person did the step and installed the held release (or a later one).
        state["update"] = {**last, "result": "resolved-by-person", "resolved_at": iso(at)}
        last = state["update"]
    if last.get("result") == "needs-person":
        return {"result": "waiting-for-person",
                "detail": "the last automatic update could not roll back by itself; "
                          "`project-observatory full update` says what to do"}
    # Every five minutes, the retry after a check that could not look included (operator rule,
    # 2026-10-17), counting the check job's own reads (`update-check.json`). A newer release the
    # job found a moment ago is not asked for again: the job started this pass to apply it.
    recent = last_check(state, read_check(base))
    is_due = due({"check": recent}, "check", CHECK_EVERY, at, CHECK_SLACK)
    pending = recent.get("result") == "update-available" and _newer(recent.get("latest"))
    if not is_due and not pending:
        return {"result": "not-due"}
    if pending and not is_due:
        # The job's own read stands, with its own time: nothing was read now (review L8).
        code, report, err = UPDATE_AVAILABLE, {"target": recent.get("latest")}, ""
        check = {"at": recent.get("at"), "exit": code, "source": "check-job"}
    else:
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
        check["failures"] = int(recent.get("failures") or 0) + 1 if recent.get("result") == "undetermined" else 1
    state["check"] = check
    if code != UPDATE_AVAILABLE:
        return {"result": check["result"]}
    if last.get("result") == "held" and check.get("latest") == last.get("to"):
        # Downloaded and verified once already; the step is still the person's.
        state["update"] = {**last, "checked_at": iso(at)}
        return {"result": "held", "detail": last.get("needs_person") or ""}
    retry_at = _retry_after(last, check.get("latest"))
    if retry_at is not None and at < retry_at:
        return {"result": "waiting-to-retry",
                "detail": f"the last attempt at {check.get('latest')} failed; tried again after {iso(retry_at)}"}
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
               UPDATE_HELD: "held", UPDATE_NO_SPACE: "refused-disk"}.get(code, "failed")
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
    """This workspace's tick and server: launchd, Task Scheduler or systemd; None without one."""
    import engine_update
    if sys.platform == "darwin":
        return engine_update.LaunchdServices() if shutil.which("launchctl") else None
    services = engine_update.OsscheduleServices()
    return services if services.available() else None


def step_jobs(launch=None, base: Path | None = None) -> dict:
    """The tick and server jobs run from the definition the installer would write now (0.19.3, OBS-393).

    Before 0.19.3 their plists could name the installer's interpreter instead, and a venv rebuilt
    on another Python release left them naming `lib/python3.X/site-packages` files that no longer
    exist (OBS-393). An update restarts them from the files as they are, so an install that had
    it wrong stayed wrong. A definition that differs from the installer's in a key it writes
    (`install_launchd.repair_definitions`, `repair_systemd` on Linux) is rewritten: the tick only
    while it is not running (otherwise the next pass). A launchd job that was loaded is reloaded and
    one that was not stays unloaded; a running systemd server is restarted, which costs its clients
    a reconnect. A drift that cannot be mended here is answered under `skipped`, with its reason."""
    recover = False
    if launch is None:
        if sys.platform == "darwin" and shutil.which("launchctl") is not None:
            launch, recover = _install_launchd(), True
        elif sys.platform.startswith("linux") and shutil.which("systemctl") is not None:
            return _step_systemd_jobs(_install_launchd())
        else:
            return {"result": "not-applicable"}
    out = _step_launchd_jobs(launch)
    if recover:
        try:
            lost = recover_lost_server(base or config.home(), launch)
        except Exception as exc:  # noqa: BLE001 — a recovery never fails a pass
            lost = {"job": "server", "result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        if lost:
            out["recovered"] = lost
    return out


#: The release whose job repair could leave the server unloaded after its update (OBS-410).
LOST_SERVER_RELEASE = "0.35.6"
#: Written once the recovery below has run, so a server a person boots out later stays out.
LOST_SERVER_MARKER = "server-recovery-obs410.json"


def recover_lost_server(base: Path, launch, *, descriptor: Path | None = None, override=None) -> dict | None:
    """Load the server 0.35.6 left unloaded after its update, once (OBS-410).

    0.35.6's `maintain ensure` rewrote the server's definition and sent `bootstrap` while launchd was
    still finishing the bootout, so the server stayed unloaded; an update from 0.35.6 runs 0.35.6's own
    code, which only restarts what is loaded, and a pass leaves an unloaded job alone. A server whose
    definition and Fabric descriptor are both in place but whose label is not loaded was lost, not
    turned off (`serverd.py --uninstall` removes both), when this workspace was updated to 0.35.6. A label the operator
    disabled (Fabric Dashboards' stop, LC-14) stays off. Every decided outcome writes the marker, so the check runs
    until it decides once and never again; only a load launchd refused is tried at the next pass."""
    marker = base / "store" / "raw" / LOST_SERVER_MARKER  # paths-check: allow — the workspace `base` passed in, which need not be this process's paths.HOME; a test redirects it through `base`
    if marker.exists():
        return None
    try:
        lines = (base / "store" / "logs" / "update.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    updated = False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "updated" and row.get("to") == LOST_SERVER_RELEASE:
            updated = True
            break
    if not updated:
        return None
    import fabric_service
    import service_identity
    job = next(j for j in launch.managed_jobs(base) if j["name"] == "server")
    described = descriptor or fabric_service.descriptor_path(service_identity.SERVICE_ID,
                                                             service_identity.instance(base))

    def done(result: str) -> None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), "result": result}) + "\n", encoding="utf-8")

    if not (Path(job["plist"]).exists() and described.exists()):
        done("no-server")
        return None
    if launch.job_loaded(job["label"]):
        done("loaded")
        return None
    if override is None:
        override = fabric_service.launchd_override
    if override(job["label"]) == "disabled":
        done("disabled")
        return None
    ok, detail = launch.start_job(job["plist"])
    if ok:
        done("loaded-again")
        return {"job": "server", "result": "loaded-again"}
    return {"job": "server", "result": "load-failed", "detail": str(detail)[:200]}


def _step_launchd_jobs(launch) -> dict:
    try:
        drift = launch.repair_interpreters(write=False)
        skipped = [{"job": j["name"], "missing": j.get("missing", []), "reason": j["skipped"]}
                   for j in drift if j.get("skipped")]
        drift = [j for j in drift if not j.get("skipped")]
        if not drift:
            return {"result": "skipped" if skipped else "ok", **({"skipped": skipped} if skipped else {})}
        ready = [j for j in drift if not (j["name"] == "tick" and launch.job_running(j["label"]))]
        if ready:
            launch.repair_interpreters(write=True, only={j["name"] for j in ready})
        fixed = []
        for job in ready:
            row = {"job": job["name"], "was": job["was"], "now": job["now"]}
            if launch.job_loaded(job["label"]):
                stopped, detail = launch.stop_job(job["label"])
                ok, detail = launch.start_job(job["plist"]) if stopped else (False, detail)
                row.update(reloaded=ok, **({} if ok else {"detail": str(detail)[:200]}),
                           **({} if stopped else {"still_loaded": True}))
            else:
                # Not loaded before (a person stopped it, or it was never started): the definition is
                # mended and stays unloaded.
                row["reloaded"] = False
                row["loaded"] = False
            fixed.append(row)
        waiting = [j["name"] for j in drift if j not in ready]
        failed = [r for r in fixed if r.get("reloaded") is False and r.get("loaded") is not False]
        lost = [r["job"] for r in failed if not r.get("still_loaded")]
        stale = [r["job"] for r in failed if r.get("still_loaded")]
        result = "reload-failed" if failed else ("repaired" if fixed else "waiting")
        return {"result": result, "jobs": fixed, **({"unloaded": lost} if lost else {}),
                **({"not_reloaded": stale} if stale else {}),
                **({"waiting": waiting} if waiting else {}), **({"skipped": skipped} if skipped else {})}
    except Exception as exc:  # noqa: BLE001 — a repair never fails a pass
        return {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _step_systemd_jobs(launch) -> dict:
    """`step_jobs` on Linux: the tick's and the server's systemd user units (OBS-393)."""
    try:
        rows = {row["name"]: row["handle"] for row in launch.systemd_unit_files()}
        drift = launch.repair_systemd(write=False)
        skipped = [{"job": j["name"], "missing": j.get("missing", []), "reason": j["skipped"]}
                   for j in drift if j.get("skipped")]
        drift = [j for j in drift if not j.get("skipped")]
        if not drift:
            return {"result": "skipped" if skipped else "ok", **({"skipped": skipped} if skipped else {})}
        ready = [j for j in drift if not (j["name"] == "tick" and rows["tick"].running())]
        fixed = []
        if ready:
            launch.repair_systemd(write=True, only={j["name"] for j in ready})
            for job in ready:
                row = {"job": job["name"], "changed": job["changed"]}
                if job["name"] == "server" and rows["server"].running():
                    ok, detail = rows["server"].restart()
                    row.update(restarted=ok, **({} if ok else {"detail": str(detail)[:200]}))
                fixed.append(row)
        waiting = [j["name"] for j in drift if j not in ready]
        return {"result": "repaired" if fixed else "waiting", "jobs": fixed,
                **({"waiting": waiting} if waiting else {}), **({"skipped": skipped} if skipped else {})}
    except Exception as exc:  # noqa: BLE001: a repair never fails a pass
        return {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


def snapshot_due(base: Path, state: dict, at: datetime.datetime) -> bool:
    """Whether the daily snapshot is due. An update's before-upgrade snapshot counts for it only
    while no secret store is kept outside the workspace: that snapshot never carries one
    (`update-snapshot`), so with an update a day the store was never carried and doctor's 48-hour
    warning fired (OSS-100). With such a store the daily is due a day after the newest snapshot that
    wrote a carry record, whatever the update did."""
    if due(state, "snapshot", SNAPSHOT_EVERY, at):
        return True
    if (state.get("snapshot") or {}).get("result") != "before-upgrade":
        return False
    try:
        import backup_vault
        import secret_store_carry
        home = base.resolve()
        stores = [s for s in secret_store_carry.external_stores(base) if not backup_vault._within(home, s["path"])]
        if not stores:
            return False
        last = parse_iso((backup_vault._last_carry(base) or {}).get("at"))
    except Exception:  # noqa: BLE001: unreadable settings keep the cadence as it was
        return False
    return last is None or at - last >= SNAPSHOT_EVERY


def step_snapshot(base: Path, state: dict, at: datetime.datetime, services=None,
                  sleep=time.sleep) -> dict:
    """A full encrypted snapshot once a day. An update's before-upgrade snapshot counts, unless a
    secret store kept outside the workspace waits to be carried (`snapshot_due`)."""
    if not snapshot_due(base, state, at):
        return {"result": "not-due"}
    # A FAILED snapshot is retried after SNAPSHOT_RETRY, not on the next pass: every attempt
    # stops the tick and the server, and with the pass every 15 minutes a lasting failure
    # took them down four times an hour (review 2026-10-12).
    failed_at = parse_iso((state.get("snapshot_failure") or {}).get("at"))
    taken_at = parse_iso((state.get("snapshot") or {}).get("at"))
    if failed_at and (taken_at is None or failed_at > taken_at) and at - failed_at < SNAPSHOT_RETRY:
        return {"result": "backing-off", "detail": "the last attempt failed; the next one is "
                                                   f"after {iso(failed_at + SNAPSHOT_RETRY)}"}
    import engine_update
    if _tick_busy(base):
        # Booting a running tick out would interrupt it every day (review F10); the next
        # pass tries again.
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
                # What the snapshot did with a secret store kept outside the workspace (OBS-350): its
                # state and reason only, so doctor and Health can say whether the newest daily carried it.
                store = result.get("secret_store") or {}
                if store.get("state") and store.get("state") != "not-applicable":
                    record["secret_store"] = {"state": store["state"], "reason": store.get("reason")}
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


def step_machine(base: Path, at: datetime.datetime, machine=None) -> dict:
    """The machine's agent setup, once a day (machine_backup.py, OBS-157). After the workspace
    snapshot, with every job running: it reads the home and the checkouts, not the workspace.
    Its own state file records each attempt, so `status` reads it whatever this pass does."""
    try:
        import machine_backup
        return (machine or machine_backup.step)(base, at)
    except Exception as exc:  # noqa: BLE001: the machine backup never stops a pass
        return {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


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


#: The disk guard's ceiling inside a pass, inside the 15-minute interval. A guard killed at it
#: stops between removals of regenerable things; the next pass starts over from a fresh plan.
GUARD_TIMEOUT = 600
#: What the guard leaves of that ceiling: it stops itself at GUARD_TIMEOUT - GUARD_MARGIN, so
#: its report and journal are written before the job would kill it (2026-10-11: the first
#: scheduled pass on 0.23.0 killed it at 1200 s and recorded only "exit 124").
GUARD_MARGIN = 120
#: The longest a pass may run before Task Scheduler ends it. NOT the interval: tied to it, the
#: 15-minute pass was killed at 14 minutes, mid-backup, with the tick and server stopped
#: (review 2026-10-12). A pass that is still running is never started twice (IgnoreNew).
PASS_TIME_LIMIT = 3540


def step_guard(base: Path, state: dict, at: datetime.datetime, runner=None) -> dict:
    """The disk guard (`tools/disk_guard.py --auto`), FIRST in a pass, before the update
    and the backup — both need space, and a full disk had them failing (OBS-40).

    Why here and not in the tick: on 2026-10-11 the tick's `machine` and `cleanup` steps
    were "not started" hour after hour, because a loaded machine spent the tick's window
    on slower collectors first; the disk fell to 1.7 GB with the self-cleaning idle for
    a day. This pass runs every 15 minutes whatever the tick does. Off unless the operator
    turned on `features.auto_cleanup` — the same switch the cleanup's auto tier obeys."""
    if not configuration_enabled(base, "auto_cleanup"):
        state["guard"] = {"at": iso(at), "result": "off"}
        return {"result": "off"}
    environ = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "OBSERVATORY_ROOT"}}
    environ["OBSERVATORY_HOME"] = str(base)
    environ[IN_PASS_ENV] = "1"
    run = runner or subprocess.run
    try:
        p = run([engine_python(), "-P", str(Path(__file__).resolve().parent / "tools" / "disk_guard.py"), "--auto", "--json",
                 "--budget", str(GUARD_TIMEOUT - GUARD_MARGIN)],
                capture_output=True, text=True, timeout=GUARD_TIMEOUT, env=environ, cwd=tempfile.gettempdir(),
                stdin=subprocess.DEVNULL)
        code, line = p.returncode, ((p.stdout or "").strip().splitlines() or [""])[-1]
        tail = ((p.stderr or "").strip().splitlines() or [""])[-1]
    except subprocess.TimeoutExpired:
        code, line, tail = 124, "", f"stopped at {GUARD_TIMEOUT} s"
    except OSError as exc:
        code, line, tail = 125, "", type(exc).__name__
    try:
        doc = json.loads(line) if line.startswith("{") else {}
    except ValueError:
        doc = {}
    # WHAT HAPPENED, said as such (review 2026-10-12): a run that was not due used to be
    # recorded as "ran" with the previous run's numbers, and one failed cache command made a
    # whole good run read "failed". Now: failed only when the guard itself did not finish.
    if code != 0 or doc.get("error") or not doc:
        outcome = "failed"
    elif doc.get("busy"):
        outcome = "busy"
    elif doc.get("not_due"):
        outcome = "not-due"
    else:
        outcome = "ran"
    result = {"result": outcome, "exit": code,
              **{k: doc[k] for k in ("level", "free_gb_before", "free_gb_after", "freed_gb", "results", "git")
                 if k in doc and outcome == "ran"},
              **({"free_gb": doc.get("free_gb_before"), "level": doc.get("level")} if outcome in ("not-due", "busy") else {}),
              **({"detail": (doc.get("error") or tail or line)[:300]} if outcome == "failed" else {})}
    state["guard"] = {"at": iso(at), **result}
    return result


def step_hung(base: Path, at: datetime.datetime, survey=None) -> dict:
    """Commands an agent session left hanging (tools/hung_commands.py, OBS-530): sampled every pass, so
    two samples are fifteen minutes apart; the leaf commands of its short list are stopped only when
    `features.auto_cleanup` is on. Never raises: a pass does not stop for it."""
    apply = configuration_enabled(base, "auto_cleanup")
    try:
        tools = str(Path(__file__).resolve().parent / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import hung_commands
        receipt = (survey or hung_commands.run)(
            base / "store" / "raw" / "hung-commands.json",  # paths-check: allow — the workspace `base` passed in, which need not be this process's paths.HOME
            base / "store" / "logs" / "hung-commands.jsonl",  # paths-check: allow — the same workspace's journal
            apply)
    except Exception as exc:  # noqa: BLE001
        return {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"result": "degraded" if receipt.get("degraded") else "ran", "hung": len(receipt.get("hung") or []),
            "stopped": sum(1 for s in receipt.get("stopped") or [] if s.get("result") == "stopped"),
            **({"degraded": receipt["degraded"]} if receipt.get("degraded") else {})}


def configuration_enabled(base: Path, name: str) -> bool:
    """A feature switch of THIS workspace, read from its own settings file."""
    try:
        doc = json.loads((base / "config" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (doc.get("features") or {}).get(name) is True


def step_org(base: Path, commands: "Commands") -> dict:
    """Organization enrollments (OBS-161): rotation before expiry, a signed check-in, share registrations, grant
    usage and the activity outbox, through `full org tick`. Nothing runs on a machine with no enrollment; a failure is
    recorded, never raised. The node folder is the one `full org` resolves (`OBSERVATORY_NODE`, else `<base>/node`),
    handed to the child explicitly so both read the same enrollments."""
    node = Path(os.environ.get("OBSERVATORY_NODE") or base / "node").expanduser()
    orgs = node / "orgs"
    if not orgs.is_dir() or not any((p / "enrollment.json").exists() for p in orgs.iterdir() if p.is_dir()):
        return {"result": "none"}
    code, doc, tail = commands.full("org", "tick", timeout=300, env={"OBSERVATORY_NODE": str(node)})
    rows = doc.get("organizations") if isinstance(doc.get("organizations"), list) else []
    summary = [{"org": r.get("org"), "result": r.get("result") or ("error" if r.get("error") else "ran")}
               for r in rows if isinstance(r, dict)]
    return {"result": "ran" if code == 0 else "error", "organizations": summary,
            **({"detail": tail} if code != 0 and tail else {})}


#: How often the maintenance pass syncs this node's shares (contract DEC-0142): the sender reads reports and seals
#: again before a sealed copy ends or after the vault value changed; the recipient renews and reports at least daily
#: (`full share sync --due` renews a received share only when its lease ended or its last report is a day old).
SHARES_EVERY = datetime.timedelta(hours=1)


def step_shares(base: Path, state: dict, at: datetime.datetime, commands: "Commands") -> dict:
    """Shares with other people's machines (OBS-264): one `full share sync --due` an hour, in a process of its own.
    Nothing runs on a machine with no share; a failure is recorded with the degraded reason `shares.unsynced`, never
    raised, and the next pass tries again. A share whose mailbox did not answer is counted, not hidden."""
    node = Path(os.environ.get("OBSERVATORY_NODE") or base / "node").expanduser()
    folders = [node / "shares" / side for side in ("sent", "received")]
    if not any(f.is_dir() and any(f.iterdir()) for f in folders):
        return {"result": "none"}
    if not due(state, "shares", SHARES_EVERY, at, slack=CHECK_SLACK):
        return {"result": "not-due", "last": (state.get("shares") or {}).get("at")}
    code, doc, tail = commands.full("share", "sync", "--due", timeout=600, env={"OBSERVATORY_NODE": str(node)})
    rows = doc.get("shares") if isinstance(doc.get("shares"), list) else []
    problems = doc.get("problems") if isinstance(doc.get("problems"), int) else None
    result = {"result": "ran" if code == 0 else "error", "shares": len(rows)}
    if code != 0:
        result.update(degraded="shares.unsynced", detail=tail)
    elif problems:
        result.update(problems=problems, degraded="shares.unsynced")
    state["shares"] = {"at": iso(at), "result": result["result"], **({"problems": problems} if problems else {})}
    return result


def run_pass(base: Path, *, at: datetime.datetime | None = None, commands: Commands | None = None,
             services=..., store=None, app=None, sleep=time.sleep, first_check_delay: float = 0,
             clock=time.monotonic, guard=step_guard, machine=None, hung=None) -> dict:
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
        try:
            report["guard"] = guard(base, state, at)
        except Exception as exc:  # noqa: BLE001 — the guard never stops an update or a backup
            report["guard"] = {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        report["hung"] = step_hung(base, at, hung)
        def settle() -> None:
            wait = first_check_delay - (clock() - PROCESS_STARTED)
            if wait > 0:
                sleep(wait)
        report["update"] = step_update(base, state, at, commands or Commands(base),
                                       before_check=settle if first_check_delay > 0 else None)
        if report["update"]["result"].startswith("updated"):
            # The new release's code is what should run next: the app and the snapshot
            # wait for the next pass, and the update's before-upgrade snapshot counts (unless an
            # external secret store waits to be carried: `snapshot_due`).
            state["snapshot"] = {"at": iso(at), "result": "before-upgrade"}
            report["next"] = "the next pass runs the new release"
        elif update_running(base):
            # AN ENGINE UPDATE IS RUNNING BESIDE THIS PASS (a person's `full update`, 2026-10-08):
            # it snapshots the workspace, and the app step rewriting store/app-update under it
            # rolled that update back. The app waits for the next pass; the snapshot defers itself.
            report["app"] = {"result": "waiting-for-engine-update"}
            report["snapshot"] = step_snapshot(base, state, at, services, sleep=sleep)  # defers itself
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
            report["machine"] = step_machine(base, at, machine)
            report["ports"] = step_ports(base)
            if services is not None:
                report["jobs"] = step_jobs(base=base)
        if not report["update"]["result"].startswith("updated"):
            report["org"] = step_org(base, commands or Commands(base))
            try:
                report["shares"] = step_shares(base, state, at, commands or Commands(base))
            except Exception as exc:  # noqa: BLE001: a share never stops the pass
                report["shares"] = {"result": "error", "degraded": "shares.unsynced",
                                    "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        # Anonymous usage and error events (OBS-324): what the commands, tools and ticks queued since
        # the last pass is sent now, in batches; never raises, a failure is a local counter.
        report["analytics"] = step_analytics(base)
        state["pass"] = {"at": iso(at)}
        report["log_retention"] = state["log_retention"] = _rotate_logs(base)
        write_state(base, state)
        report["schedule_reload"] = _reload_after_pass(base)
        return report


def step_ports(base: Path) -> dict:
    """This OS account's ports (OBS-354). An account with a record refreshes its host claim, so a cleaner that ages
    the shared folder never drops it. An installed server with no record is the migration: on a host with one account,
    or where the historical ports are this account's or free, they are recorded as they are and nothing moves; where
    another account holds them, this account takes a block and its server job is reinstalled on it (`moved`).
    Never raises: a port never stops a backup."""
    try:
        import local_ports
        have = local_ports.record(base)
        if have:
            return {"result": "kept", "scheme": have["scheme"],
                    "claim": "written" if local_ports.write_claim(have) else "not-written"}
        if local_ports.current_server_port(base) is None:
            return {"result": "no-server"}
        out = local_ports.allocate(base, reinstall=True)
        moved = (out.get("reinstall") or {}).get("status") == "reinstalled"
        return {"result": "moved" if moved else "recorded", "scheme": out["scheme"],
                "server": out["ports"]["server"], "claim": out["claim"],
                **({"reinstall": out["reinstall"]} if "reinstall" in out else {})}
    except Exception as exc:  # noqa: BLE001
        return {"result": "error", "code": getattr(exc, "code", type(exc).__name__),
                "detail": str(exc)[:200]}


def step_analytics(base: Path) -> dict:
    """Send the queued anonymous usage events (usage_analytics.flush). Never raises."""
    try:
        import usage_analytics
        return usage_analytics.flush(base)
    except Exception as exc:  # noqa: BLE001: analytics never stops a pass
        return {"result": "error", "kind": type(exc).__name__}


#: How long a release the check job handed to the pass waits before the job starts the pass again
#: for it (a deferred or refused attempt): the scheduled pass comes this often anyway.
KICK_AGAIN = datetime.timedelta(seconds=900)


def feed_latest(base: Path) -> str:
    """The newest release's version from the static feed (engine_update.latest_version)."""
    import engine_update as eu
    api, repo = eu.release_source()
    # One attempt: the fetcher sleeps between attempts, and the next check is five minutes away.
    return eu.latest_version(eu.Fetcher(timeout=20, attempts=1), api, repo)[0]


def _kick_wanted(state: dict, latest: str, at: datetime.datetime) -> bool:
    """Whether the check job should start the pass now for `latest`."""
    update = state.get("update") or {}
    if update.get("result") == "needs-person":
        return False
    if update.get("result") == "held" and update.get("to") == latest:
        return False                 # verified once; the step is a person's (LC-16)
    if state.get("update_in_flight"):
        return False
    retry_at = _retry_after(update, latest)
    if retry_at is not None and at < retry_at:
        return False
    kicked = state.get("kick") or {}
    last = parse_iso(kicked.get("at"))
    return not (kicked.get("to") == latest and last is not None and at - last < KICK_AGAIN)


@contextlib.contextmanager
def check_lock(base: Path):
    """One check job at a time per workspace. Its own lock, never the pass's: holding the pass lock
    made the 15-minute pass find it busy and skip the guard, the backup and the app (review H1)."""
    file = base / "store" / "update-check.lock"
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = osprivacy.open(file, os.O_CREAT | os.O_RDWR | osprivacy.NOFOLLOW, 0o600)
    try:
        try:
            oslocks.flock(fd, oslocks.LOCK_EX | oslocks.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def check_pass(base: Path, *, at: datetime.datetime | None = None, feed=None, kick=None) -> dict:
    """The five-minute check job (`maintain.py check`): one read of the static feed, nothing else.

    Never the pass: no guard, backup, app or update runs here, so the job ends well inside its
    five-minute interval (LC-03). It takes its own lock, never the pass's, and writes its own
    record (`update-check.json`), never maintenance.json, so a pass running beside it is neither
    skipped nor overwritten; it never sleeps. A newer release starts the pass at once through
    `kick` (the scheduler's own "run now"), which applies it at a safe point. `quiet` in the
    answer means the check found what the last one found: `main` prints nothing then."""
    at = at or now()
    config.validate_workspace(base, required=True)
    # OFF STILL ANNOUNCES (OBS-333, ADR-0188 item 4): with automatic updates off the feed is read
    # on the same schedule, nothing is downloaded or installed and the pass is never started; a
    # newer version is recorded in `available` and announced once per version.
    off = not auto_enabled(base)
    kick_to, announce = None, None
    with check_lock(base) as held:
        if not held:
            return {"status": "busy", "quiet": True}
        if update_running(base):
            return {"status": "update-running", "quiet": True}
        state, record = read_state(base), read_check(base)    # the pass's record is only read
        previous = record.get("check") or {}
        if not due({"check": last_check(state, record)}, "check", CHECK_EVERY, at, CHECK_SLACK):
            return {"status": "not-due", "quiet": True}
        import engine_update as eu
        check: dict = {"at": iso(at), "source": "feed"}
        try:
            latest = (feed or feed_latest)(base)
        except (eu.UpdateError, OSError, ValueError) as exc:
            check.update(result="undetermined", detail=str(exc)[:300],
                         failures=int(previous.get("failures") or 0) + 1 if previous.get("result") == "undetermined" else 1)
            update_events.emit("update_check", "check_failed", base=base)
        else:
            newer = _newer(latest)
            check.update(result="update-available" if newer else "up-to-date", latest=latest)
            update_events.emit("update_check", "ready" if newer else "current", base=base)
            if newer and off:
                if (record.get("available") or {}).get("version") != latest:
                    announce = record["available"] = {"version": latest, "found_at": iso(at)}
            else:
                record.pop("available", None)
            if newer and not off and _kick_wanted({**state, "kick": record.get("kick")}, latest, at):
                kick_to = latest
                record["kick"] = {"at": iso(at), "to": latest}
        record["check"] = check
        write_check(base, record)
    report: dict = {"status": "checked", "at": iso(at), "check": check}
    if off:
        report["auto_update"] = False
    if announce:
        report["announce"] = {**announce, "install": INSTALL_COMMAND,
                              "text": announcement(announce["version"])}
    if kick_to:
        try:
            report["kick"] = (kick or (lambda: schedule_for(base).kick()))()
        except Exception as exc:  # noqa: BLE001: the scheduled pass still comes within 15 minutes
            report["kick"] = {"result": "failed", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    report["quiet"] = not kick_to and not announce and previous.get("result") == check["result"] \
        and previous.get("latest") == check.get("latest")
    return report


#: How a person installs a release while automatic updates are off.
INSTALL_COMMAND = "project-observatory full update --apply"


def announcement(version: str) -> str:
    return (f"version {version} is available; automatic updates are off, so it waits for you: "
            f"`{INSTALL_COMMAND}`")


def settle_available(base: Path, installed: str | None = None) -> dict | None:
    """Keep the check record's `available` true right away, not at the next check five minutes on: it
    is dropped once updates are on (the job installs) or once `installed` (default: this engine) is
    that version or a later one. The Mac app reads the record as the engine's decision (OBS-333).
    Answers what stays recorded; never raises, under the check job's lock when it can take it."""
    try:
        with check_lock(base) as held:
            record = read_check(base)
            found = record.get("available") or {}
            if not found:
                return None
            try:
                stale = auto_enabled(base) or (
                    config.version_tuple(str(installed or config.VERSION)) >= config.version_tuple(str(found.get("version"))))
            except (ValueError, TypeError, config.ConfigurationError):
                stale = True
            if not stale:
                return found
            if held:
                record.pop("available", None)
                write_check(base, record)
            return None
    except OSError:
        return None


def available_update(base: Path) -> dict | None:
    """The newer version the check job found while automatic updates are off (OBS-333), or None:
    None while they are on (the job installs it), and once the engine runs that version or a later one."""
    if auto_enabled(base):
        return None
    found = read_check(base).get("available") or {}
    if not _newer(found.get("version")):
        return None
    return {"version": str(found["version"]), "found_at": found.get("found_at"), "install": INSTALL_COMMAND}


def _reload_after_pass(base: Path, spawn=None) -> str | None:
    """Load a schedule an update wrote but could not reload from inside the pass that ran it.

    The update's `maintain ensure` runs inside this pass, so it writes the new plist and
    leaves `maintenance-reload-pending`; before 2026-10-12 only the plugin hook or a person
    read that file, and an install without them kept the old hourly schedule. A detached
    `maintain ensure`, started now and waiting until this pass has exited, reloads it."""
    pending = base / "store" / "maintenance-reload-pending"
    if not pending.exists() or sys.platform != "darwin":
        return None
    env = {k: v for k, v in os.environ.items() if k != IN_PASS_ENV}
    env["OBSERVATORY_HOME"] = str(base)
    script = str(Path(__file__).resolve().parent / "tools" / "maintain.py")
    argv = ["/bin/sh", "-c", 'sleep 45; exec "$0" "$1" ensure', engine_python(), script]
    try:
        (spawn or subprocess.Popen)(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir(),
                                    **osproc.new_group(detached=True))
    except OSError as exc:
        return f"not started: {type(exc).__name__}"
    return "after this pass"


def run_app_step(base: Path, *, at: datetime.datetime | None = None, app=None) -> dict:
    """Only the app step of a pass, under the same lock and in the same record.

    The Mac app starts this from a detached helper once it has quit — its *Restart to
    update*, a quit, or a long idle — so a verified bundle that waited with
    `waiting-for-quit` is swapped in now instead of at the next pass. Nothing else
    a pass does (the engine update, the backup, the passphrase) runs here."""
    at = at or now()
    config.validate_workspace(base, required=True)
    if sys.platform != "darwin" and app is None:
        return {"status": "skipped", "detail": "the app exists only on macOS"}
    with pass_lock(base) as held:
        if not held:
            return {"status": "busy", "detail": "another maintenance pass holds the lock"}
        if update_running(base):
            return {"status": "ran", "at": iso(at), "app": {"result": "waiting-for-engine-update"}}
        state = read_state(base)
        try:
            import app_update
            result = (app or app_update.AppUpdater(base)).step(state, at)
        except Exception as exc:  # noqa: BLE001 — recorded, as in a full pass
            result = {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
        write_state(base, state)
        return {"status": "ran", "at": iso(at), "app": result}


def _rotate_logs(base: Path, *, at=None) -> dict:
    """Independent scope receipts; a failed rotation never suppresses its peers."""
    import log_policy
    receipt = {"schema": "log-retention/1", "checked_at": iso(at or now()), "scopes": {}}
    state_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    scopes = {"engine": lambda: base / "store" / "logs",
              "updater": update_events.directory,
              "hooks": lambda: state_dir / "project-observatory"}
    # The workspace is the engine scope's trusted root: links at or above it (macOS /tmp,
    # /var, a linked home) are followed once, links inside it refuse.
    roots = {"engine": base}
    for name, directory in scopes.items():
        checked_at = iso(at or now())
        try:
            receipt["scopes"][name] = log_policy.sweep_report(directory(), root=roots.get(name))
        except Exception:  # rotation must not fail maintenance or leak external diagnostics
            receipt["scopes"][name] = log_policy.unknown_report()
        receipt["scopes"][name]["checked_at"] = checked_at
    return receipt


def log_retention_status(raw, *, at=None) -> dict:
    """Whitelist stored receipt fields; old/malformed state never becomes fresh evidence."""
    import log_policy
    at = at or now()
    raw = raw if isinstance(raw, dict) else {}
    stamp = parse_iso(raw.get("checked_at")) if isinstance(raw.get("checked_at"), str) else None
    age = (at - stamp).total_seconds() if stamp else None
    valid_time = age is not None and age >= 0
    sources = raw.get("scopes") if isinstance(raw.get("scopes"), dict) else {}
    result = {"checked_at": iso(stamp) if valid_time else None,
              "age_seconds": int(age) if valid_time else None,
              "stale": not valid_time or age > 3600, "scopes": {}}
    def count(v):
        return type(v) is int and v >= 0
    for name in ("engine", "updater", "hooks"):
        row = sources.get(name)
        valid = (raw.get("schema") == "log-retention/1" and valid_time and isinstance(row, dict)
                 and all(k in row for k in ("status", "observed_count", "rotated", "refused", "failed", "reasons"))
                 and row.get("status") in ("complete", "partial", "unknown")
                 and (row.get("observed_count") is None or count(row.get("observed_count")))
                 and all(count(row.get(k)) or (row["status"] == "unknown" and row.get(k) is None)
                         for k in ("rotated", "refused", "failed"))
                 and isinstance(row.get("reasons"), dict)
                 and all(k in log_policy.REASONS and count(v) and v > 0 for k, v in row["reasons"].items())
                 and (row["status"] != "complete" or (count(row["observed_count"])
                      and not row["refused"] and not row["failed"] and not row["reasons"]))
                 and (row["status"] != "unknown" or row["observed_count"] is None))
        result["scopes"][name] = ({k: row[k] for k in ("status", "observed_count", "rotated", "refused", "failed", "reasons")}
                                    if valid else log_policy.unknown_report("receipt_unreadable"))
        if valid and row.get("absent") is True and row["status"] == "complete" and row["observed_count"] == 0:
            result["scopes"][name]["absent"] = True
        measured = parse_iso(row.get("checked_at")) if isinstance(row, dict) else None
        age_scope = (at - measured).total_seconds() if measured else None
        if age_scope is None or age_scope < 0:
            result["scopes"][name] = log_policy.unknown_report("receipt_unreadable")
            measured, age_scope = None, None
        result["scopes"][name].update(checked_at=iso(measured) if measured else None,
                                     age_seconds=int(age_scope) if age_scope is not None else None,
                                     stale=age_scope is None or age_scope > 3600)
    states = [row["status"] for row in result["scopes"].values()]
    result["stale"] = result["stale"] or any(row["stale"] for row in result["scopes"].values())
    result["status"] = "complete" if all(s == "complete" for s in states) else (
        "unknown" if all(s == "unknown" for s in states) else "partial")
    return result


# --- the schedule ------------------------------------------------------------------

def _install_launchd():
    sys.path.insert(0, str(config.SOURCE / "tools"))
    import install_launchd
    return install_launchd


def _instance_digest(base: Path) -> str:
    return hashlib.sha256(str(base.resolve()).encode()).hexdigest()[:16]


#: The two jobs of a workspace: the pass, and the five-minute check (operator rule, 2026-10-17).
JOBS = {"maintain": {"action": "run", "interval": INTERVAL_SECONDS, "limit": None, "what": "maintenance"},
        "check": {"action": "check", "interval": CHECK_INTERVAL_SECONDS, "limit": CHECK_TIME_LIMIT,
                  "what": "update check"}}


class LaunchdSchedule:
    """org.project-observatory.<sha16>.maintain: every 15 minutes, at login too, never a managed job;
    `job="check"`: org.project-observatory.<sha16>.check, every five minutes."""

    kind = "launchd"

    def __init__(self, base: Path, runner=None, job: str = "maintain", headless: bool | None = None):
        self.base, self.job = base, job
        self.launch = _install_launchd()
        self.label = f"org.project-observatory.{_instance_digest(base)}.{job}"
        self.plist = Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"
        self.runner = runner
        self._headless = headless

    def headless(self) -> bool:
        """This account has no GUI session (OBS-361 part 2): `bootstrap gui/<uid>` fails with 125 there, so
        the job is a system daemon with UserName set to this account, which root's space_daemons.py loads
        from the background folder. Asked once per schedule, as machine-move cutover asks it."""
        if self._headless is None:
            import machine_move
            if sys.platform != "darwin" or shutil.which("launchctl") is None:
                self._headless = False
            else:
                try:
                    self._headless = not machine_move.gui_session(self.launch.uid(), self.runner or subprocess.run)
                except (OSError, subprocess.SubprocessError):
                    # launchctl did not answer: keep the agent path, which fails and says so, as before.
                    self._headless = False
        return self._headless

    #: Where root's space_daemons.py writes the daemon it loads (root:wheel 0644, readable by all).
    DAEMONS = Path("/Library/LaunchDaemons")

    def root_copy_stale(self, doc: dict) -> bool:
        """Root's loaded daemon was made from another definition than DOC: its program or working
        folder differs (space_daemons.py copies both unchanged). False when it cannot be read, so
        the answer is never invented (OBS-393)."""
        path = self.DAEMONS / f"{self.label}.plist"
        try:
            daemon = plistlib.loads(path.read_bytes()) if path.is_file() else None
        except (OSError, ValueError, plistlib.InvalidFileException):
            daemon = None
        if not isinstance(daemon, dict):
            return False
        return any(daemon.get(key) != doc.get(key) for key in ("ProgramArguments", "WorkingDirectory"))

    def background_definition(self) -> Path:
        """The definition root's space_daemons.py loads: the account's background folder."""
        import machine_identity
        return machine_identity.background_dir(Path.home()) / f"{self.label}.plist"

    @property
    def definition(self) -> Path:
        """Where the definition lives: ~/Library/LaunchAgents, or the background folder when headless."""
        if self.headless():
            return self.background_definition()
        return self.plist

    def document(self, kept_from: Path | None = None) -> dict:
        env = self.launch.environment()
        # PATH comes from whoever runs the installer, and a terminal and an agent session
        # differ: an installed job keeps the PATH it has, so the definition does not flip
        # (and get reloaded) with each caller (review F1). KEPT_FROM names the definition the
        # document is compared with (root's background copy), so the PATH is read from that one.
        source = kept_from or (self.definition if self.definition.is_file() else self.plist)
        if source.is_file():
            try:
                kept = (plistlib.loads(source.read_bytes()).get("EnvironmentVariables") or {}).get("PATH")
            except (OSError, ValueError, plistlib.InvalidFileException):
                kept = None
            # Never a Cellar or another account's folder, even when an older release kept one (OBS-393).
            kept = self.launch.kept_path(kept)
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
            # Version-free (OBS-393): the venv's stable interpreter and the launcher's program name,
            # never a path inside `lib/python3.X/site-packages`, which a venv rebuilt on another Python
            # release no longer has (launchd failed every run with exit 78). A checkout names the file.
            "ProgramArguments": self.launch.job_argv("maintain", *self.arguments(),
                                                     python=self.launch.stable_interpreter(engine_python())),
            "StartInterval": JOBS[self.job]["interval"],
            # The check job does not run at load: the pass does (its check 90 s after the login),
            # and the check's first run is one interval later, never the pass's moment (review H1).
            "RunAtLoad": self.job == "maintain",
            # The workspace home, never site-packages; the launcher starts the program in the engine.
            "WorkingDirectory": str(self.launch.job_cwd(self.base)),
            "StandardOutPath": str(logs / f"{self.job}.log"),
            "StandardErrorPath": str(logs / f"{self.job}.err"),
            # Not "Background" with LowPriorityIO: on a loaded machine that throttling
            # stretched a 2-second `full update --check` past its 600-second limit (seen
            # live, 2026-10-05). A nice value is enough.
            "ProcessType": "Standard",
            "Nice": 10,
            "EnvironmentVariables": env,
            "Umask": 0o077,
        }

    def arguments(self) -> list[str]:
        """The program's own arguments: the action, and for the pass its first-check delay."""
        return [JOBS[self.job]["action"],
                *(["--first-check-delay", str(FIRST_CHECK_DELAY)] if self.job == "maintain" else [])]

    def installed(self) -> bool:
        return self.definition.is_file()

    def system_daemon(self) -> bool:
        """Root's space_daemons.py runs this label as a system daemon of this account (OBS-361 part 2).
        False where it cannot be asked (Windows, launchctl that did not answer)."""
        if not hasattr(os, "getuid"):
            return False
        import machine_move
        try:
            return machine_move._system_daemon_loaded(self.runner or subprocess.run, self.label, self.launch.uid())
        except (OSError, subprocess.SubprocessError):
            return False

    def loaded(self) -> bool:
        if self.headless():
            return self.system_daemon()
        return self.launch.job_loaded(self.label) or self.system_daemon()

    def install(self) -> dict:
        doc = self.document()
        problems = self.launch.lint_plist(doc)
        if problems:
            raise config.ConfigurationError("; ".join(problems))
        self.launch.prepare_logs((f"{self.job}.log", f"{self.job}.err"), self.base / "store" / "logs")
        if self.headless():
            return self._install_background(doc)
        if self.system_daemon():
            # Root's daemon of this account runs it already (a space signed in at the screen later):
            # an agent loaded too would run every pass twice (OBS-361 part 2), so it is booted out.
            if self.launch.job_loaded(self.label):
                self.launch.stop_job(self.label)
            background = self.background_definition()
            try:
                current = plistlib.loads(background.read_bytes()) if background.is_file() else None
            except (OSError, ValueError, plistlib.InvalidFileException):
                current = None
            # Compared with the document whose PATH is the background copy's own, so a caller with
            # another PATH does not read as a change (and a waiting-for-root) on every ensure.
            doc = self.document(kept_from=background) if background.is_file() else doc
            if (current is not None and current != doc) or self.root_copy_stale(doc):
                # Root's daemon was made from a definition the builder no longer writes (a path of a
                # venv rebuilt on another Python, OBS-393): only root can replace it, remove then apply.
                return self._install_background(doc, target=background)
            return {"schedule": self.kind, "label": self.label, "changed": False, "domain": "system",
                    "load": "loaded"}
        current = plistlib.loads(self.plist.read_bytes()) if self.installed() else None
        pending = self.base / "store" / "maintenance-reload-pending"
        if current == doc and self.loaded() and (self.job != "maintain" or not pending.exists()):
            return {"schedule": self.kind, "label": self.label, "changed": False}
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self._write(self.plist, doc)
        if self.loaded():
            if self.job == "maintain" and pass_running(self.base):
                # Booting the job out now would kill the pass that is running — perhaps the
                # very `full update` asking for this schedule. The new definition is on disk;
                # the next ensure outside a pass (the plugin hook, a person) loads it.
                pending.write_text(iso(now()) + "\n")
                return {"schedule": self.kind, "label": self.label, "changed": True, "reload": "deferred"}
            self.launch.stop_job(self.label)
        ok, detail = self.launch.start_job(str(self.plist))
        if not ok:
            raise config.ConfigurationError(f"launchd did not load {self.label}: {detail[:200]}")
        if self.job == "maintain":
            pending.unlink(missing_ok=True)
        return {"schedule": self.kind, "label": self.label, "changed": True}

    @staticmethod
    def _write(target: Path, doc: dict) -> None:
        if target.is_symlink():
            raise config.ConfigurationError("LaunchAgent plist cannot be a symbolic link")
        tmp = target.with_suffix(".plist.tmp")
        tmp.unlink(missing_ok=True)
        fd = osprivacy.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(plistlib.dumps(doc))
        os.replace(tmp, target)

    def _install_background(self, doc: dict, target: Path | None = None) -> dict:
        """`install` with no GUI session (OBS-361 part 2): the definition goes to the background folder, and the
        administrator loads it with space_daemons.py. A copy left in ~/Library/LaunchAgents by an earlier
        attempt is removed, so a later sign-in at the screen never starts the job a second time."""
        import machine_identity
        import machine_move
        home, uid = Path.home(), self.launch.uid()
        machine_identity.ensure_private_dir(machine_identity.background_dir(home), home)
        target = target or self.definition
        try:
            current = plistlib.loads(target.read_bytes()) if target.is_file() else None
        except (OSError, ValueError, plistlib.InvalidFileException):
            current = None
        changed = current != doc
        if changed:
            self._write(target, doc)
        elif target.is_file() and not target.is_symlink():
            # Root refuses a definition anyone but this account can change (space_daemons.py).
            target.chmod(target.stat().st_mode & 0o7755 & ~0o022)
        loaded = self.loaded()
        # launchctl also answers 125 for an ordinary account signed out at the screen but reached over SSH:
        # its LaunchAgents copy loads at the next sign-in, so it is kept, and refreshed, until root's daemon
        # runs; only then is it removed, so a sign-in never starts the job a second time.
        agent_copy = self.plist.is_file() and not self.plist.is_symlink()
        if agent_copy and loaded:
            self.plist.unlink()
        elif agent_copy:
            try:
                if plistlib.loads(self.plist.read_bytes()) != doc:
                    self._write(self.plist, doc)
            except (OSError, ValueError, plistlib.InvalidFileException):
                self._write(self.plist, doc)
        out = {"schedule": self.kind, "label": self.label, "changed": changed, "domain": "system",
               "definition": str(target), "load": "loaded" if loaded else "waiting-for-root",
               **({"removed_agent_copy": str(self.plist)} if agent_copy and loaded else {}),
               **({"agent_copy_kept": str(self.plist)} if agent_copy and not loaded else {})}
        if loaded and not changed and not self.root_copy_stale(doc):
            # Root's daemon runs this very definition. One it loaded from an earlier definition still
            # waits for root's remove, then apply, though the background copy is already the new one.
            return out
        # root's apply refuses a label that is already loaded: a new definition is removed, then applied.
        steps = ([machine_move.root_step("remove", self.label, uid)] if loaded else []) + [
            machine_move.root_step("apply", self.label, uid)]
        out["load"] = "waiting-for-root"
        out["result"] = "waiting-for-root"
        out["next"] = steps
        out["human_steps"] = ["if the administrator has not installed it on this computer yet: "
                              + "; ".join(machine_move.install_steps())] + [
            f"then: {step}  (this account has no GUI session, so the job runs as a system daemon with "
            "UserName set to this account, never as root)" for step in steps]
        return out

    def kick(self) -> dict:
        """Run the job now (`launchctl kickstart`, no `-k`: a running pass is left alone)."""
        if self.headless() or self.system_daemon():
            # A system daemon: only root can kickstart it, and the pass runs at its next interval anyway.
            return {"result": "failed", "detail": f"{self.label} is a system daemon of this account; "
                                                  f"it runs at its next interval (sudo launchctl kickstart system/{self.label} "
                                                  "starts it now)"}
        ok, detail = self.launch.kick_job(self.label)
        return {"result": "started" if ok else "failed", **({} if ok else {"detail": detail[:200]})}

    def uninstall(self) -> dict:
        if self.headless() or self.system_daemon():
            import machine_move
            if self.launch.job_loaded(self.label):
                self.launch.stop_job(self.label)
            loaded = self.system_daemon()
            self.definition.unlink(missing_ok=True)
            self.plist.unlink(missing_ok=True)
            out = {"schedule": self.kind, "label": self.label, "removed": True, "domain": "system"}
            if loaded:
                out["next"] = [machine_move.root_step("remove", self.label, self.launch.uid())]
            return out
        if self.loaded():
            self.launch.stop_job(self.label)
        self.plist.unlink(missing_ok=True)
        return {"schedule": self.kind, "label": self.label, "removed": True}

    def status(self) -> dict:
        out = {"kind": self.kind, "label": self.label, "installed": self.installed(), "loaded": self.loaded()}
        if not self.headless():
            # One `launchctl print` answers the pid and the last exit: a job launchd keeps loaded
            # while every start exits 78 read as scheduled (2026-10-29).
            state = self.launch.job_state(self.label)
            out.update(pid=state["pid"], last_exit=state["last_exit"])
            if out["loaded"] and self.system_daemon():
                out.update(installed=True, domain="system")
            return out
        out["domain"] = "system"
        if out["installed"] and not out["loaded"]:
            import machine_move
            out["next"] = machine_move.root_step("apply", self.label, self.launch.uid())
        return out


class SystemdSchedule:
    """A systemd user timer: every 15 minutes, 90 seconds after the user manager starts (LC-16)."""

    kind = "systemd"

    def __init__(self, base: Path, runner=subprocess.run, which=shutil.which, job: str = "maintain"):
        self.base, self.runner, self.which, self.job = base, runner, which, job
        self.name = f"project-observatory-{_instance_digest(base)}-{job}"
        root = os.environ.get("XDG_CONFIG_HOME")
        self.units = (Path(root) if root and Path(root).is_absolute() else Path.home() / ".config") / "systemd" / "user"

    @staticmethod
    def quote(value: str) -> str:
        # `%` is a systemd specifier and `$` expands in ExecStart: a path holding either
        # would run something else than it names, so it is refused rather than escaped.
        if any(c in value for c in '"\\\n%$'):
            raise config.ConfigurationError("A path with a quote, backslash, newline, % or $ cannot go in a systemd unit")
        return f'"{value}"'

    @classmethod
    def directory(cls, value: str) -> str:
        # systemd reads the rest of a WorkingDirectory= line as the path and does not unquote it, so
        # `WorkingDirectory="/x"` names a folder whose name holds the quotes and the unit fails with
        # `bad-setting` (OBS-503). A space needs no escaping there; what `quote` refuses is refused
        # here too, and so is a path that is not absolute or that ends in white space.
        cls.quote(value)
        if not value.startswith("/") or value != value.rstrip():
            raise config.ConfigurationError("A WorkingDirectory must be an absolute path with no trailing white space")
        return value

    def service_text(self) -> str:
        launch = _install_launchd()
        argv = launch.job_argv("maintain", JOBS[self.job]["action"],
                               python=launch.stable_interpreter(engine_python()))
        return "\n".join([
            "[Unit]",
            (f"Description=Project Observatory maintenance (updates and backups) for {self.base}"
             if self.job == "maintain" else f"Description=Project Observatory update check for {self.base}"),
            "",
            "[Service]",
            "Type=oneshot",
            # Version-free (OBS-393), as the launchd job: no path inside `lib/python3.X`.
            "ExecStart=" + " ".join(self.quote(a) for a in argv[:-1]) + f" {argv[-1]}",
            f"WorkingDirectory={self.directory(str(launch.job_cwd(self.base)))}",
            f"Environment={self.quote('OBSERVATORY_HOME=' + str(self.base))}",
            'Environment="OBSERVATORY_SYSTEM_SETUP=1"',
            "Nice=10",
            "UMask=0077",
            *([f"TimeoutStartSec={CHECK_TIME_LIMIT}"] if self.job == "check" else []),
            # The pass starts an update in a session of its own and never waits to kill
            # it (audit A03); systemd's default control-group kill would take it down with
            # the pass on a stop or a logout, leaving the jobs it stopped down (A03 review).
            "KillMode=process",
            "",
        ])

    def timer_text(self) -> str:
        return "\n".join([
            "[Unit]",
            f"Description=Run Project Observatory {JOBS[self.job]['what']} every "
            f"{JOBS[self.job]['interval'] // 60} minutes for {self.base}",
            "",
            "[Timer]",
            # The user manager starts at login: the first pass, and so the first check,
            # comes 90 seconds after it (LC-16), then every 15 (the check: 5) minutes.
            f"OnStartupSec={FIRST_CHECK_DELAY if self.job == 'maintain' else CHECK_FIRST_DELAY}s",
            f"OnUnitActiveSec={JOBS[self.job]['interval'] // 60}min",
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

    def kick(self) -> dict:
        """Run the pass now; `--no-block` returns at once, the timer keeps its cadence."""
        code, out = self._systemctl("start", "--no-block", f"{self.name}.service")
        return {"result": "started" if code == 0 else "failed", **({} if code == 0 else {"detail": out[:200]})}


class TaskSchedulerSchedule:
    """A per-user Windows task: every 15 minutes, and 90 seconds after logon (LC-16), never a managed job;
    the same pass the launchd agent and the systemd timer run, through `osschedule`."""

    kind = "task-scheduler"

    def __init__(self, base: Path, runner=None, job: str = "maintain"):
        import osschedule
        self.base = base
        launch = _install_launchd()
        python = launch.stable_interpreter(engine_python())
        job = osschedule.Job(
            name=job, workspace=base,
            argv=tuple(launch.job_argv("maintain", JOBS[job]["action"], python=python)),
            env=(("OBSERVATORY_HOME", str(base)), ("OBSERVATORY_PYTHON", python), ("OBSERVATORY_SYSTEM_SETUP", "1")),
            cwd=launch.job_cwd(base), stdout=base / "store" / "logs" / f"{job}.log", stderr=base / "store" / "logs" / f"{job}.err",
            interval_seconds=JOBS[job]["interval"], at_login=True,
            first_delay_seconds=FIRST_CHECK_DELAY if job == "maintain" else CHECK_FIRST_DELAY,
            phase_seconds=0 if job == "maintain" else CHECK_FIRST_DELAY,
            # The pass is short; an update it starts runs in a process of its own, which ending
            # the task does not stop.
            time_limit_seconds=JOBS[job]["limit"] or PASS_TIME_LIMIT)
        self.job = osschedule.TaskSchedulerJob(job, **({"runner": runner} if runner else {}))

    def installed(self) -> bool:
        return self.job.installed()

    def install(self) -> dict:
        import osschedule
        try:
            return self.job.install()
        except osschedule.ScheduleError as exc:
            raise config.ConfigurationError(str(exc)) from None

    def uninstall(self) -> dict:
        return self.job.uninstall()

    def status(self) -> dict:
        return self.job.status()

    def kick(self) -> dict:
        ok, detail = self.job.start()
        return {"result": "started" if ok else "failed", **({} if ok else {"detail": str(detail)[:200]})}


class Schedules:
    """The workspace's two jobs as one schedule: the pass and the five-minute check.

    Scheduled means both are; an install from before the check job reads as not scheduled
    until `maintain ensure` (which every `full update` runs) adds it."""

    def __init__(self, main, check):
        self.main, self.check = main, check
        self.kind = main.kind

    def install(self) -> dict:
        out = self.main.install()
        return {**out, "check": self.check.install()}

    def uninstall(self) -> dict:
        out = self.main.uninstall()
        return {**out, "check": self.check.uninstall()}

    def status(self) -> dict:
        main, check = self.main.status(), self.check.status()
        # `maintain` keeps the pass job's own state: the merged `loaded` is false while the check job
        # is not loaded, and would hide a pass job whose every start exits 78 (review of the job-exit row).
        out = {**main, "check": check, "maintain": main}
        if not out.get("next") and check.get("next"):
            out["next"] = check["next"]
        for key in ("installed", "loaded", "active"):
            if key in main and main[key] is not None:
                out[key] = bool(main[key]) and bool(check.get(key))
        return out

    def kick(self) -> dict:
        return self.main.kick()


def schedule_for(base: Path):
    if sys.platform == "darwin":
        kind = LaunchdSchedule
    elif sys.platform == "win32":
        kind = TaskSchedulerSchedule
    else:
        kind = SystemdSchedule
    return Schedules(kind(base), kind(base, job="check"))


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
    probe = schedule is None
    schedule = schedule or schedule_for(base)
    try:
        out["schedule"] = schedule.install()
    except (config.ConfigurationError, OSError) as exc:
        out["schedule"] = {"result": "unscheduled", "detail": str(exc)[:300],
                           "fallback": "the observatory-log plugin runs the pass at each new session"}
    if probe and probes_allowed():
        # The tick and the server too (OBS-393): a definition the installer would no longer write (a
        # path of a venv rebuilt on another Python) is rewritten and reloaded here, so a person's
        # `maintain ensure` mends every job, not only the two it schedules.
        out["jobs"] = step_jobs(base=base)
        out["hook_env"] = _refresh_hook_env()
    return out


def _refresh_hook_env() -> dict:
    """The plugin hooks' environment in Claude Code's settings, rewritten when it names an engine
    folder or interpreter that is gone (OBS-393, `agent_plugin.refresh_hook_env`)."""
    try:
        _install_launchd()
        import agent_plugin
        return agent_plugin.refresh_hook_env()
    except Exception as exc:  # noqa: BLE001 - scheduling never fails on the hooks' settings
        return {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}


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

def _definition_files(base: Path) -> list[tuple[str, Path]]:
    """(job, file) for every job definition of this workspace on this machine that exists: the pass, the
    check, the tick and the server (launchd's agent folder and the background folder root loads from,
    or systemd user units). Labels and unit names come from BASE, the workspace asked about."""
    found: list[tuple[str, Path]] = []
    if sys.platform == "darwin":
        import machine_identity
        background = machine_identity.background_dir(Path.home())
        for job in JOBS:
            label = f"org.project-observatory.{_instance_digest(base)}.{job}"
            for folder in (Path.home() / "Library" / "LaunchAgents", background):
                found.append((job, folder / f"{label}.plist"))
        try:
            launch = _install_launchd()
            found += [(row["name"], Path(row["plist"])) for row in launch.managed_jobs(base)]
        except Exception:  # noqa: BLE001 - a status never fails doctor
            pass
    elif sys.platform != "win32":
        for job in JOBS:
            units = SystemdSchedule(base, job=job)
            found.append((job, units.units / f"{units.name}.service"))
        try:
            found += [(row["name"], row["service"]) for row in _install_launchd().systemd_unit_files(base)]
        except Exception:  # noqa: BLE001 - a status never fails doctor
            pass
    return [(job, path) for job, path in found if path.is_file() and not path.is_symlink()]


def _systemd_paths(text: str) -> dict:
    """ExecStart's words and WorkingDirectory of a unit file, in the plist's shape, for the same check."""
    doc: dict = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "ExecStart":
            try:
                doc["ProgramArguments"] = shlex.split(value)
            except ValueError:
                pass
        elif key == "WorkingDirectory":
            doc["WorkingDirectory"] = value.strip().strip('"')
    return doc


def stale_definitions(base: Path) -> list[dict]:
    """The job definitions that name a program, an argument or a working folder that does not exist
    (OBS-393): `[{job, definition, missing}]`. Read from the files; nothing is asked of launchd."""
    out: list[dict] = []
    try:
        files = _definition_files(base)
        launch = _install_launchd()
    except Exception:  # noqa: BLE001 - a status never fails doctor
        return out
    for job, path in files:
        try:
            raw = path.read_bytes()
            doc = plistlib.loads(raw) if path.suffix == ".plist" else _systemd_paths(raw.decode("utf-8", "replace"))
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        missing = launch.missing_paths(doc)
        if missing:
            out.append({"job": job, "definition": str(path), "missing": missing})
    return out


def _venv_problem() -> str | None:
    try:
        return _install_launchd().venv_home_problem()
    except Exception:  # noqa: BLE001 - a status never fails doctor
        return None


#: Backup warning codes a maintenance item already says in its own words (OBS-349).
BACKUP_TWINS = frozenset({"backups-no-passphrase", "backups-unencrypted-local"})

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
    elif not scheduled and sched.get("domain") == "system" and sched.get("installed"):
        warn("waiting-for-root", "updates and daily backups wait for an administrator: this account has no GUI "
                                 f"session, so its job runs as a system daemon once root loads it: {sched.get('next')}",
             step=sched.get("next"))
    elif not scheduled:
        warn("not-scheduled", "updates and daily backups are not scheduled on this machine yet: "
                              "`project-observatory full maintain ensure`")
    stale = stale_definitions(base)
    if stale:
        named = "; ".join(f"{row['job']} ({', '.join(row['missing'][:3])})" for row in stale)
        warn("job-path-missing", f"a background job's definition names a path that does not exist, so launchd or "
                                 f"systemd cannot start it (a virtual environment rebuilt on another Python leaves "
                                 f"this): {named}; `project-observatory full maintain ensure` rewrites them",
             step="project-observatory full maintain ensure", jobs=[row["job"] for row in stale])
    venv = _venv_problem()
    if venv:
        # No step: the engine cannot rebuild the environment it runs in; a person recreates it.
        warn("venv-home-missing", f"{venv}. Its interpreter may still start through another link; if the jobs "
                                  "fail, recreate the environment from an installed Python, install the engine into "
                                  "it again, then run `project-observatory full maintain ensure`")
    for job, part in (("maintain", sched.get("maintain") or sched), ("check", sched.get("check") or {})):
        last = part.get("last_exit")
        if isinstance(last, int) and last > 0:
            problem = _install_launchd().job_problem(job, {"loaded": part.get("loaded"), "pid": part.get("pid"),
                                                           "last_exit": last})
            if problem:
                warn("job-exit", problem, job=job, exit=last)
    if not auto_enabled(base):
        warn("auto-off", f"{SWITCH_LABEL}: off — new releases wait for you: `project-observatory full auto-update on`")
    available = available_update(base)
    if available:
        warn("update-available", announcement(available["version"]), version=available["version"])
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
                         f"({update.get('detail')}); it is tried again on the next pass",
             version=update.get("to"), detail=str(update.get("detail") or ""))
    if update.get("result") == "needs-person":
        warn("needs-person", "the last automatic update needs a person: run `project-observatory full update`")
    elif update.get("failures"):
        hour = _transient(update)
        detail = str(update.get("detail", ""))[:120]
        warn("update-incomplete", f"the last automatic update did not complete ({update.get('result')}: "
                                  f"{detail}); it is tried again {'at the next pass' if hour else 'six hours after it failed'}",
             result=update.get("result"), detail=detail, soon=hour)
    failure = state.get("snapshot_failure") or {}
    if failure and (parse_iso(failure.get("at")) or now()) >= (parse_iso((state.get("snapshot") or {}).get("at"))
                                                              or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)):
        detail = str(failure.get("detail", ""))[:160]
        warn("snapshot-failed", f"the last daily backup failed: {detail}; it is tried again on the next pass",
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
    retention = log_retention_status(state.get("log_retention"))
    if retention["stale"] or retention["status"] != "complete":
        warn("log-retention-incomplete", "log retention evidence is incomplete or older than one hour; "
             "inspect scope results with `project-observatory full maintain status`")
    # The machine bundle (OBS-157): its warnings join the job's, with their own codes.
    try:
        import machine_backup
        machine = machine_backup.status(base)
    except Exception as exc:  # noqa: BLE001: a status never fails doctor
        machine = {"error": f"{type(exc).__name__}"}
    for item in machine.get("warning_items") or []:
        warnings.append(item["text"])
        items.append(item)
    # The backups' own warnings (OBS-349), with their codes, except the two a maintenance item
    # above already says: no passphrase (`passphrase`, `not-encrypted`) and unencrypted local
    # copies (`not-encrypted`). The rest (a secret store left out of the snapshot, a copy left by a
    # rolled-back update, a root inside the workspace) no other item names.
    try:
        import backup_vault
        backups = backup_vault.status(base)
    except Exception:  # noqa: BLE001: a status never fails doctor
        backups = {}
    for item in backups.get("warning_items") or []:
        if item.get("code") in BACKUP_TWINS:
            continue
        warnings.append(item["text"])
        items.append(item)
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
            "last_pass": (state.get("pass") or {}).get("at"), "check": last_check(state, read_check(base)) or None,
            "update": state.get("update"), "update_in_flight": state.get("update_in_flight"),
            "snapshot": state.get("snapshot"), "snapshot_failure": state.get("snapshot_failure"),
            "machine_backup": machine,
            "app": state.get("app"), "log_retention": retention,
            **({"available": available} if available else {}),
            "warnings": warnings, "warning_items": items}


# --- a reinstall restores ------------------------------------------------------------

def restore_latest(base: Path, *, store=None, label: str | None = None, same_machine: bool | None = None,
                   second_machine: bool | None = None, unknown: str = "second", stdin_value: str | None = None,
                   ask: bool = False, secret_store: str = "auto", secret_store_names: bool = False,
                   secret_store_target: str | None = None, secret_store_verify_into=None) -> dict:
    """Restore the newest backup a workspace at this path left (D7). Only into an empty home.

    When backups of more than one workspace exist under this name (a reinstall that could
    not find its passphrase starts a second, empty workspace with its own daily backups),
    nothing is chosen: the answer lists them and `--label` picks one (audit A06). Within
    one workspace the newest snapshot that opens is restored, falling back to older ones.

    Each snapshot is opened with `backup_vault.open_with`: OBSERVATORY_BACKUP_PASSPHRASE,
    `stdin_value` (`--passphrase-stdin`) and every passphrase the OS store keeps under the
    folder's label. A backup copied from another machine sits under THAT machine's label,
    which this store does not hold (OBS-348): with `ask` (`full restore --latest`, never
    `init`) and a terminal, the person is asked once per label and that label's snapshots are
    tried again with the answer. The value is used for this restore and kept nowhere.

    Nothing found for this exact path: the answer is `no-backup`, plus `elsewhere` when the
    same workspace left backups under another home (`backup_vault.elsewhere`, OBS-347), with
    the explicit-file step; nothing is chosen.

    `secret_store` is the policy for a secret store the snapshot carried (OBS-350): `full init` passes
    `keep`, so a reinstall over a surviving store never fails or overwrites it. A refusal of the store
    (`secret-store-not-empty`, ...) is raised as it is: an older backup would meet the same store.
    `secret_store_verify_into` verifies the carried stores into that folder instead (OBS-394,
    `workspace_upgrade.restore`)."""
    import secret_store_carry
    import workspace_upgrade
    if base.exists() and any(p.name != ".workspace.lock" for p in base.iterdir()):
        raise config.ConfigurationError("Restore requires a new empty destination; existing state is never overwritten")
    if secret_store_verify_into is not None:
        # Before any passphrase is asked (OBS-394): a contradiction and a place that is never written.
        secret_store_carry.verify_conflict(secret_store, secret_store_target)
        secret_store_carry.check_verify_into(secret_store_verify_into, [(base, "the new home")], require_empty=False)
    found = backup_vault.candidates(base)
    if label:
        found = [c for c in found if c["label"] == label]
        if not found:
            return {"status": "no-backup", "detail": f"no backups under the label {label!r}"}
    if not found:
        out = {"status": "no-backup", "detail": f"no backups of a workspace named {base.name!r} were found"}
        other = backup_vault.elsewhere(base)
        if other:
            snap = other[0]["newest"]
            out.update({"elsewhere": other,
                        "detail": out["detail"] + " at this path; the same workspace left backups under another "
                                                  "home, listed in `elsewhere`, and none is chosen",
                        "next": f"project-observatory full restore '{snap}'"})
        return out
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
    use_store = store if store is not None or os.environ.get("OBSERVATORY_SYSTEM_SETUP") == "0" \
        else backup_vault.SecretStore()
    problems: list[dict] = []
    tried_any = False

    def attempt(candidate: dict, typed: str | None = None) -> dict | None:
        """The restore of one snapshot, or None after recording why it did not happen."""
        nonlocal tried_any
        try:
            if typed is None:
                secret, _, source = backup_vault.open_with(
                    candidate["snapshot"], base=None, label=candidate["label"] if use_store is not None else None,
                    stdin_value=stdin_value, store=use_store, verify=False)
            elif backup_vault.opens(candidate["snapshot"], typed):
                secret, source = typed, "prompt"
            else:
                problems.append({"snapshot": candidate["snapshot"].name, "code": "passphrase-refused",
                                 "detail": "the passphrase typed does not open it"})
                return None
        except backup_vault.BackupError as exc:
            tried_any = tried_any or bool(getattr(exc, "tried", None))
            problems.append({"snapshot": candidate["snapshot"].name, "code": exc.code or "unreadable",
                             "detail": str(exc)[:200]})
            return None
        tried_any = True
        try:
            result = workspace_upgrade.restore(candidate["snapshot"], base, secret=secret, prompt=False,
                                               same_machine=same_machine, second_machine=second_machine,
                                               unknown=unknown, secret_store=secret_store,
                                               secret_store_names=secret_store_names,
                                               secret_store_target=secret_store_target,
                                               secret_store_verify_into=secret_store_verify_into)
        except secret_store_carry.CarryRefused as exc:
            if exc.code != "secret-store-manifest-mismatch":
                raise
            problems.append({"snapshot": candidate["snapshot"].name, "code": exc.code, "detail": str(exc)[:200]})
            return None
        except (backup_vault.BackupError, config.ConfigurationError, OSError) as exc:
            # A damaged copy or one that needs a newer release: try the next older backup (review F10).
            problems.append({"snapshot": candidate["snapshot"].name, "detail": str(exc)[:200]})
            if not base.exists() or not any(p.name != ".workspace.lock" for p in base.iterdir()):
                return None
            raise
        result.update({"status": "restored", "from": str(candidate["snapshot"]), "label": candidate["label"],
                       "passphrase_source": source})
        if problems:
            result["skipped"] = problems
        return result

    for candidate in found:
        result = attempt(candidate)
        if result is not None:
            return result
    refused = {p["snapshot"] for p in problems if p.get("code") == "passphrase-refused"}
    locked = [lbl for lbl in labels if any(c["snapshot"].name in refused for c in found if c["label"] == lbl)]
    if ask and locked:
        for lbl in locked:   # newest first; one question per label, never one per snapshot
            typed = backup_vault.ask(lbl)
            if not typed:
                continue
            for candidate in (c for c in found if c["label"] == lbl):
                result = attempt(candidate, typed)
                if result is not None:
                    return result
    newest = found[0]
    # The command a person can run as given: an EMPTY home (a fresh `init` already made
    # this one non-empty), and no passphrase on the command line: `restore` asks for it
    # at a terminal (audit A05), or reads it from a file on stdin (OBS-348).
    by_hand = f"project-observatory --home NEW_EMPTY_HOME full restore '{newest['snapshot']}'"
    by_stdin = f"project-observatory --home NEW_EMPTY_HOME full restore '{newest['snapshot']}' --passphrase-stdin < FILE"
    if tried_any and not all(p.get("code") == "passphrase-refused" or "authentication" in p["detail"]
                             for p in problems):
        return {"status": "restore-failed", "newest": str(newest["snapshot"]), "skipped": problems,
                "next": "`project-observatory full init --fresh` starts empty; the backups are left as they are"}
    return {"status": "backups-found-locked", "newest": str(newest["snapshot"]),
            "detail": "no passphrase that opens these backups was found in the OS credential store; a backup "
                      "another machine took opens with that machine's passphrase",
            "next": by_hand + " (it asks for the passphrase at a terminal); without a terminal, redirect the "
                              "other machine's passphrase file: " + by_stdin}


# --- CLI ------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project-observatory full maintain",
                                 description="updates that arrive by themselves and a daily backup")
    ap.add_argument("action", choices=["run", "check", "ensure", "uninstall", "status", "hook", "app"])
    ap.add_argument("--if-wanted", action="store_true",
                    help="with ensure: keep a schedule a person removed with `maintain uninstall` removed")
    ap.add_argument("--first-check-delay", type=float, default=0, metavar="SECONDS",
                    help="with run or check: a check waits until SECONDS after this process started (the scheduler's)")
    return ap


def hook(base: Path) -> dict:
    """What the observatory-log plugin runs, detached, at a session's start.

    It schedules the job where nothing is scheduled yet (respecting `maintain uninstall`),
    and where no scheduler exists — Linux without a systemd user manager — it runs the
    pass itself, which is cheap when nothing is due. With automatic updates off the pass
    looks for nothing, so the hook also runs the check there: a newer version is still
    announced (OBS-333), and the check never starts or installs anything while off."""
    result = ensure(base)
    sched = result.get("schedule") or {}
    # In an account with no GUI session the job waits for root's space_daemons.py (OBS-361 part 2);
    # until root loads it, the hook keeps running the pass, as it does where nothing is scheduled.
    if sched.get("result") in ("unscheduled", "waiting-for-root"):
        result["pass"] = run_pass(base)
        if not auto_enabled(base):
            result["check"] = check_pass(base)
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
        if a.action in ("run", "check", "hook") and not system_setup_allowed(base):
            raise config.ConfigurationError(
                f"`maintain {a.action}` touches launchd/systemd and the credential store; this process "
                "may not (OBSERVATORY_SYSTEM_SETUP=0, a temporary workspace, or not this account's home). "
                "Run it from a terminal, or with OBSERVATORY_SYSTEM_SETUP=1")
        if a.action == "run":
            # launchd and systemd stop a job with SIGTERM; the default handler would skip
            # the `finally` that starts the tick and server again (review F2).
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
            result = run_pass(base, first_check_delay=max(0.0, min(a.first_check_delay, 600.0)))
        elif a.action == "check":
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
            result = check_pass(base)
            if result.pop("quiet", False):
                # Nothing new: the log keeps a change, not every five minutes (operator rule).
                return 0
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
            settle_available(base)
        result = {"auto_update": auto_enabled(base), "switch": switch_status(base)}
        available = available_update(base)
        if available:
            result["available"] = available
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
