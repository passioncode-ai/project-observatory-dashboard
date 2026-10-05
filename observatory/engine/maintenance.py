"""Updates that arrive by themselves, and a daily backup that leaves the workspace.

The decisions behind it are in docs/runs/2026-10-05-auto-update (D1–D8). One job per
workspace runs `tools/maintain.py run` every hour — launchd on macOS, a systemd user
timer on Linux — and each pass does only what is due:

1. a backup passphrase exists and is kept outside the workspace (backup_vault.SecretStore);
2. once a day, with `updates.auto` on (the default): `full update --check`, and when a
   newer stable release exists, `full update --apply` — verified and reversible, the
   same transaction a person runs. A failure is recorded and retried the next day; a
   rollback that needs a person stops the automatic attempts until one succeeds;
3. macOS: the app follows the engine (app_update.py), never while it runs;
4. once a day: a full encrypted snapshot of the workspace (settings, registry, vault,
   store), with this workspace's tick and server stopped for the copy.

    full auto-update status|on|off     the switch (`updates.auto`); off keeps the backups
    full maintain run                  one pass now (what the scheduler runs)
    full maintain ensure               schedule the pass and mirror the passphrase
    full maintain uninstall            remove the schedule; it stays off until `ensure`
    full maintain status               what ran, when, and what it found
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
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import configuration as config
import backup_vault

STATE_FILE = "maintenance.json"
CHECK_EVERY = datetime.timedelta(hours=24)
SNAPSHOT_EVERY = datetime.timedelta(hours=24)
INTERVAL_SECONDS = 3600
#: Exit codes of `full update` (engine_update.EXIT_*), named here so a change there is
#: caught by tests/test_maintenance.py rather than by a user's stalled install.
UPDATE_OK, UPDATE_FAILED, UPDATE_REFUSED, UPDATE_UNDETERMINED = 0, 1, 2, 3
UPDATE_NEEDS_PERSON, UPDATE_SERVICES, UPDATE_AVAILABLE = 4, 5, 10
SNAPSHOT_ATTEMPTS = 3
#: Set in every process a maintenance pass starts. A schedule installed from inside a pass
#: never boots the job out: that would kill the pass and whatever it runs (review F1).
IN_PASS_ENV = "OBSERVATORY_MAINTENANCE_PASS"
SNAPSHOT_KIND = "daily"


# --- the switch -------------------------------------------------------------------

def settings(base: Path) -> dict:
    return config.load(base).get("updates", {})


def auto_enabled(base: Path) -> bool:
    """`updates.auto` is on unless a person turned it off (D1)."""
    return settings(base).get("auto", True) is not False


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
    try:
        temp = Path(tempfile.gettempdir()).resolve()
        resolved = base.resolve()
        if resolved == temp or temp in resolved.parents or Path("/private/tmp") in resolved.parents \
                or Path("/tmp") in resolved.parents:
            return False
    except OSError:
        return False
    return sys.stdin is not None and sys.stdin.isatty()


def engine_python() -> str:
    """The interpreter this engine is installed in, whoever started this process.

    The plugin hook may run under any `python3` on PATH; a job or a `full update` started
    with that one would not find the installed package. The engine's own virtual
    environment is the ancestor holding `pyvenv.cfg`; a checkout has `.venv`. Its
    `bin/python` is named as it is — never resolved through the symlink to a versioned
    Homebrew path (install_launchd.stable_interpreter)."""
    for parent in config.SOURCE.parents:
        if (parent / "pyvenv.cfg").is_file():
            for name in ("python3", "python"):
                candidate = parent / "bin" / name
                if candidate.exists():
                    return str(candidate)
    checkout = config.SOURCE / ".venv" / "bin" / "python"
    return str(checkout) if checkout.exists() else sys.executable


class Commands:
    """The engine's own CLI, run in a new process with the installed code."""

    def __init__(self, base: Path, python: str | None = None, runner=subprocess.run):
        self.base, self.python, self.runner = base, python or engine_python(), runner

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


def step_update(base: Path, state: dict, at: datetime.datetime, commands: Commands) -> dict:
    """Check once a day; apply a newer stable release. Returns what happened."""
    if not auto_enabled(base):
        return {"result": "off"}
    last = state.get("update") or {}
    if last.get("result") == "needs-person" and last.get("from") != config.VERSION:
        # A person ran `full update` (or reinstalled): the engine is no longer the one that
        # needed them, so the automatic attempts resume (review F3).
        state["update"] = {**last, "result": "resolved-by-person", "resolved_at": iso(at)}
        last = state["update"]
    if last.get("result") == "needs-person":
        return {"result": "waiting-for-person",
                "detail": "the last automatic update could not roll back by itself; "
                          "`project-observatory full update` says what to do"}
    if not due(state, "check", CHECK_EVERY, at):
        return {"result": "not-due"}
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
    state["check"] = check
    if code != UPDATE_AVAILABLE:
        return {"result": check["result"]}
    if update_running(base):
        # A person (or the plugin's bridge) is updating right now; never a second transaction.
        return {"result": "another-update-running"}
    code, report, err = commands.full("update", "--apply", timeout=3600,
                                      env={"OBSERVATORY_SYSTEM_SETUP": "1"})
    outcome = {UPDATE_OK: "updated", UPDATE_SERVICES: "updated-services-not-restarted",
               UPDATE_FAILED: "failed-rolled-back", UPDATE_REFUSED: "refused",
               UPDATE_UNDETERMINED: "undetermined", UPDATE_NEEDS_PERSON: "needs-person"}.get(code, "failed")
    update = {"at": iso(at), "exit": code, "result": outcome, "from": config.VERSION,
              "to": report.get("version") or check.get("latest")}
    if code not in (UPDATE_OK, UPDATE_SERVICES):
        update["detail"] = err or f"exit {code}"
        update["failures"] = int((state.get("update") or {}).get("failures") or 0) + 1
    state["update"] = update
    return {"result": outcome}


def _services():
    """This workspace's launchd tick and server, or None where there are none (Linux)."""
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        return None
    import engine_update
    return engine_update.LaunchdServices()


def step_snapshot(base: Path, state: dict, at: datetime.datetime, services=None,
                  sleep=time.sleep) -> dict:
    """A full encrypted snapshot once a day. An update's before-upgrade snapshot counts."""
    if not due(state, "snapshot", SNAPSHOT_EVERY, at):
        return {"result": "not-due"}
    import workspace_upgrade
    if _tick_busy(base):
        # Booting a running tick out would interrupt it every day (review F10); the next
        # hourly pass tries again.
        return {"result": "deferred", "detail": "a tick or another workspace operation is running"}
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
                return {"result": "taken", "encrypted": record["encrypted"]}
            except config.ConfigurationError as exc:
                # A session MCP server's journal line, or a tick still releasing its lock.
                last = str(exc)
                sleep(10 * (attempt + 1))
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
             services=..., store=None, app=None, sleep=time.sleep) -> dict:
    """One pass of everything that is due. Never raises for a step's own failure: each
    is recorded in store/maintenance.json and named in the answer."""
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
        report["update"] = step_update(base, state, at, commands or Commands(base))
        if report["update"]["result"].startswith("updated"):
            # The new release's code is what should run next: the app and the snapshot
            # wait for the next pass, and the update's before-upgrade snapshot counts.
            state["snapshot"] = {"at": iso(at), "result": "before-upgrade"}
            report["next"] = "the next pass runs the new release"
        else:
            if sys.platform == "darwin" or app is not None:
                try:
                    import app_update
                    report["app"] = (app or app_update.AppUpdater(base)).step(state, at)
                except Exception as exc:  # noqa: BLE001 — the app never stops a backup
                    report["app"] = {"result": "error", "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
            report["snapshot"] = step_snapshot(base, state, at, services, sleep=sleep)
        state["pass"] = {"at": iso(at)}
        write_state(base, state)
        return report


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
            "ProgramArguments": [self.launch.stable_interpreter(engine_python()),
                                 str(config.SOURCE / "tools" / "maintain.py"), "run"],
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": True,
            "WorkingDirectory": str(config.SOURCE),
            "StandardOutPath": str(logs / "maintain.log"),
            "StandardErrorPath": str(logs / "maintain.err"),
            "ProcessType": "Background",
            "LowPriorityIO": True,
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
    """A systemd user timer: hourly, 10 minutes after boot, catching up a missed run."""

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
            "IOSchedulingClass=idle",
            "UMask=0077",
            "",
        ])

    def timer_text(self) -> str:
        return "\n".join([
            "[Unit]",
            f"Description=Run Project Observatory maintenance hourly for {self.base}",
            "",
            "[Timer]",
            "OnBootSec=10min",
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
    set_setting(base, "scheduled", False)
    return (schedule or schedule_for(base)).uninstall()


# --- what doctor and the Health page read -------------------------------------------

def status(base: Path, *, schedule=None) -> dict:
    """Whether updates and backups run by themselves, and what they last found. No value."""
    state = read_state(base)
    try:
        sched = (schedule or schedule_for(base)).status()
    except Exception as exc:  # noqa: BLE001 — a status never fails doctor
        sched = {"error": f"{type(exc).__name__}"}
    warnings = []
    scheduled = bool(sched.get("installed")) and sched.get("loaded", sched.get("active", True)) is not False
    if not schedule_wanted(base):
        warnings.append("the maintenance schedule was turned off; updates and daily backups do not run by "
                        "themselves: `project-observatory full maintain ensure`")
    elif not scheduled:
        warnings.append("updates and daily backups are not scheduled on this machine yet: "
                        "`project-observatory full maintain ensure`")
    if not auto_enabled(base):
        warnings.append("automatic updates are off: `project-observatory full auto-update on`")
    update = state.get("update") or {}
    if state.get("services_not_restarted") or update.get("result") == "updated-services-not-restarted":
        warnings.append("a background job did not start again after the last backup or update: "
                        "`project-observatory full doctor` names it; `install_launchd.py install` and "
                        "`serverd.py --install` start them")
    if (state.get("app") or {}).get("result") == "refused":
        warnings.append(f"the Mac app update was refused: {(state.get('app') or {}).get('detail', '')}"[:300])
    if update.get("result") == "needs-person":
        warnings.append("the last automatic update needs a person: run `project-observatory full update`")
    elif update.get("failures"):
        warnings.append(f"the last automatic update failed ({update.get('result')}); it is retried daily")
    snapshot = state.get("snapshot") or {}
    if snapshot.get("result") == "taken" and snapshot.get("encrypted") is False:
        warnings.append("the last daily backup is not encrypted and stays inside the workspace: "
                        + (snapshot.get("detail") or "no passphrase"))
    snap = parse_iso(snapshot.get("at"))
    if snap is None or now() - snap > SNAPSHOT_EVERY * 2:
        warnings.append("no full backup in the last two days")
    return {"auto_update": auto_enabled(base), "schedule_wanted": schedule_wanted(base), "schedule": sched,
            "last_pass": (state.get("pass") or {}).get("at"), "check": state.get("check"),
            "update": state.get("update"), "snapshot": state.get("snapshot"), "app": state.get("app"),
            "warnings": warnings}


# --- a reinstall restores ------------------------------------------------------------

def restore_latest(base: Path, *, store=None) -> dict:
    """Restore the newest backup a workspace at this path left (D7). Only into an empty home."""
    import workspace_upgrade
    if base.exists() and any(p.name != ".workspace.lock" for p in base.iterdir()):
        raise config.ConfigurationError("Restore requires a new empty destination; existing state is never overwritten")
    found = backup_vault.candidates(base)
    if not found:
        return {"status": "no-backup", "detail": f"no backups of a workspace named {base.name!r} were found"}
    store = store or backup_vault.SecretStore()
    env_secret = os.environ.get(backup_vault.PASS_ENV)
    problems: list[dict] = []
    for candidate in found:
        secret = env_secret or store.get(candidate["label"])
        if not secret:
            continue
        try:
            result = workspace_upgrade.restore(candidate["snapshot"], base, secret=secret)
        except (backup_vault.BackupError, config.ConfigurationError, OSError) as exc:
            # A wrong passphrase, a damaged copy or one that needs a newer release: try the
            # next older backup rather than leave the person with nothing (review F10).
            problems.append({"snapshot": candidate["snapshot"].name, "detail": str(exc)[:200]})
            if not base.exists() or not any(p.name != ".workspace.lock" for p in base.iterdir()):
                continue
            raise
        result.update({"status": "restored", "from": str(candidate["snapshot"]), "label": candidate["label"]})
        if problems:
            result["skipped"] = problems
        return result
    newest = found[0]
    if problems and not all("authentication" in p["detail"] for p in problems):
        return {"status": "restore-failed", "newest": str(newest["snapshot"]), "skipped": problems,
                "next": "`project-observatory full init --fresh` starts empty; the backups are left as they are"}
    return {"status": "backups-found-locked", "newest": str(newest["snapshot"]),
            "detail": "no passphrase for these backups was found in the OS credential store",
            "next": f"OBSERVATORY_BACKUP_PASSPHRASE=… project-observatory full restore '{newest['snapshot']}'"}


# --- CLI ------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="project-observatory full maintain",
                                 description="updates that arrive by themselves and a daily backup")
    ap.add_argument("action", choices=["run", "ensure", "uninstall", "status", "hook"])
    ap.add_argument("--if-wanted", action="store_true",
                    help="with ensure: keep a schedule a person removed with `maintain uninstall` removed")
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
                                 description="install each new stable release by itself (on by default)")
    ap.add_argument("action", choices=["status", "on", "off"])
    return ap


def main(argv: list[str]) -> int:
    a = parser().parse_args(argv)
    try:
        base = config.home()
        if a.action == "run":
            # launchd and systemd stop a job with SIGTERM; the default handler would skip
            # the `finally` that starts the tick and server again (review F2).
            signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
            result = run_pass(base)
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
        if a.action in ("on", "off"):
            set_setting(base, "auto", a.action == "on")
        result = {"auto_update": auto_enabled(base)}
        if a.action == "on" and system_setup_allowed(base):
            result["ensure"] = ensure(base, explicit=True)
        elif a.action == "status":
            result = status(base)
        print(json.dumps(result, indent=2))
        return 0
    except (config.ConfigurationError, OSError) as exc:
        print(f"Observatory: {exc}", file=sys.stderr)
        return 2
