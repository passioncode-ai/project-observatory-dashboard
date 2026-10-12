"""One OS seam for the engine's background jobs: the tick, the server, the maintenance pass.

docs/design/WINDOWS-LINUX.md, W4. A job is described once (`Job`) and installed by the system's
own supervisor, per user, never with administrator rights and never with a stored password:

* **Windows — Task Scheduler.** A task under `\\ProjectObservatory\\`, created from XML with
  `schtasks /Create /XML`, run as this account only while it is logged on (`InteractiveToken`),
  started by `pythonw.exe` (no console window) through `tools/scheduled.py`, which sets the
  environment, the working folder and the log files the task itself cannot (fabric-service DEC-0032:
  a logon trigger, restart on failure every minute, no execution time limit for a service).
* **Linux — systemd user units** under `$XDG_CONFIG_HOME/systemd/user`: a oneshot service and a
  timer for a periodic job, a `Restart=always` service for the server (DEC-0032: `RestartSec=10`,
  `TimeoutStopSec` above the drain, `WantedBy=default.target`).
* **macOS — launchd** stays in `tools/install_launchd.py` and `tools/serverd.py`.

Nothing here carries a secret: a task file and a unit are readable by other local tools, so the
environment a job gets names folders and the interpreter only, as the launchd plists do.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

ENGINE = Path(__file__).resolve().parent
WRAPPER = ENGINE / "tools" / "scheduled.py"
TASK_FOLDER = "\\ProjectObservatory\\"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class ScheduleError(RuntimeError):
    """The supervisor refused or is missing; the message is one sentence for a person."""


def instance(workspace: Path) -> str:
    """The workspace's 16-hex digest, as in the launchd labels (`install_launchd.instance_label`)."""
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class Job:
    """What runs, how often, and where it writes. `argv[0]` is the engine's interpreter."""

    name: str                      # tick | server | maintain
    workspace: Path
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: Path
    stdout: Path
    stderr: Path
    interval_seconds: int | None = None    # periodic job; None = a service that stays up
    at_login: bool = False
    first_delay_seconds: int = 0
    time_limit_seconds: int | None = None  # None: no limit (a service)
    background: bool = True

    def __post_init__(self):
        if self.name not in ("tick", "server", "maintain"):
            raise ValueError(f"unknown job {self.name!r}")
        if self.interval_seconds is not None and self.interval_seconds < 60:
            raise ValueError("a periodic job runs at most once a minute")
        for key, value in self.env:
            if "\n" in key or "\n" in value or "=" in key:
                raise ValueError(f"environment entry {key!r} cannot go in a job definition")

    @property
    def service(self) -> bool:
        return self.interval_seconds is None


# --- Windows: Task Scheduler ----------------------------------------------------------------

def _iso_duration(seconds: int) -> str:
    """`PT…` the way Task Scheduler writes it; 0 means no limit there."""
    if seconds <= 0:
        return "PT0S"
    minutes, rest = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") + (f"{rest}S" if rest else "")


def windowless(python: str) -> str:
    """`pythonw.exe` beside `python.exe` when there is one: a task started by python.exe opens a
    console window on the person's desktop every time it runs."""
    p = Path(python)
    candidate = p.with_name("pythonw" + p.suffix) if p.stem.lower() == "python" else None
    return str(candidate) if candidate and candidate.exists() else python


class TaskSchedulerJob:
    kind = "task-scheduler"

    def __init__(self, job: Job, *, user: str | None = None, runner=subprocess.run, which=shutil.which):
        self.job, self.runner, self.which = job, runner, which
        self.user = user
        self.path = f"{TASK_FOLDER}{instance(job.workspace)}-{job.name}"
        self.stamp = Path(job.workspace) / "store" / "schedule" / f"{job.name}.task.sha256"

    # The task's command line: the wrapper under pythonw, then the job's own argv.
    def command(self) -> tuple[str, list[str]]:
        python = self.job.argv[0]
        args = [str(WRAPPER), "--cwd", str(self.job.cwd), "--stdout", str(self.job.stdout),
                "--stderr", str(self.job.stderr)]
        # A Windows console and a redirected handle default to the ANSI code page; the engine's
        # Russian log lines and page text need UTF-8 (W7).
        env = dict(self.job.env)
        env.setdefault("PYTHONUTF8", "1")
        for key, value in sorted(env.items()):
            args += ["--env", f"{key}={value}"]
        return windowless(python), args + ["--", python, *self.job.argv[1:]]

    def xml(self) -> str:
        j = self.job
        program, args = self.command()
        triggers = []
        if j.at_login or j.service:
            delay = f"<Delay>{_iso_duration(j.first_delay_seconds)}</Delay>" if j.first_delay_seconds else ""
            who = f"<UserId>{escape(self.user)}</UserId>" if self.user else ""
            triggers.append(f"<LogonTrigger><Enabled>true</Enabled>{who}{delay}</LogonTrigger>")
        if j.interval_seconds:
            # A fixed start in the past: the repetition is anchored there and never drifts with
            # each reinstall. No Duration: it repeats indefinitely.
            triggers.append("<TimeTrigger><StartBoundary>2026-01-01T00:00:00</StartBoundary><Enabled>true</Enabled>"
                            f"<Repetition><Interval>{_iso_duration(max(60, j.interval_seconds))}</Interval>"
                            "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition></TimeTrigger>")
        restart = ("<RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>"
                   if j.service else "")
        limit = _iso_duration(j.time_limit_seconds or 0)
        principal_user = f"<UserId>{escape(self.user)}</UserId>" if self.user else ""
        return "\n".join([
            '<?xml version="1.0" encoding="UTF-16"?>',
            f'<Task version="1.4" xmlns="{TASK_NAMESPACE}">',
            f"  <RegistrationInfo><Description>{escape(f'Project Observatory {j.name} for {j.workspace}')}</Description>"
            "<Author>project-observatory</Author></RegistrationInfo>",
            f"  <Triggers>{''.join(triggers)}</Triggers>",
            f'  <Principals><Principal id="Author">{principal_user}<LogonType>InteractiveToken</LogonType>'
            "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>",
            "  <Settings>",
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            "    <StartWhenAvailable>true</StartWhenAvailable>",
            "    <AllowStartOnDemand>true</AllowStartOnDemand>",
            "    <Enabled>true</Enabled>",
            "    <Hidden>true</Hidden>",
            f"    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>",
            f"    <Priority>{7 if j.background else 5}</Priority>",
            f"    {restart}" if restart else "",
            "    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>",
            "  </Settings>",
            '  <Actions Context="Author"><Exec>'
            f"<Command>{escape(program)}</Command>"
            f"<Arguments>{escape(subprocess.list2cmdline(args))}</Arguments>"
            f"<WorkingDirectory>{escape(str(j.cwd))}</WorkingDirectory>"
            "</Exec></Actions>",
            "</Task>",
            "",
        ])

    def _schtasks(self, *args: str, timeout: int = 60) -> tuple[int, str]:
        if self.which("schtasks") is None:
            return 127, "schtasks is not on PATH"
        try:
            p = self.runner(["schtasks", *args], capture_output=True, text=True, timeout=timeout,
                            encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, type(exc).__name__
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()

    def installed(self) -> bool:
        return self._schtasks("/Query", "/TN", self.path)[0] == 0

    loaded = installed

    def install(self) -> dict:
        text = self.xml()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self.stamp.is_file() and self.stamp.read_text().strip() == digest and self.installed():
            return {"schedule": self.kind, "task": self.path, "changed": False}
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(text.encode("utf-16"))   # with a BOM, as the declaration says
            code, out_text = self._schtasks("/Create", "/TN", self.path, "/XML", tmp, "/F")
        finally:
            os.unlink(tmp)
        if code != 0:
            raise ScheduleError(f"Task Scheduler did not create {self.path}: {out_text[:200]}")
        self.stamp.parent.mkdir(parents=True, exist_ok=True)
        self.stamp.write_text(digest + "\n")
        return {"schedule": self.kind, "task": self.path, "changed": True}

    def uninstall(self) -> dict:
        if self.installed():
            self._schtasks("/End", "/TN", self.path)
            self._schtasks("/Delete", "/TN", self.path, "/F")
        self.stamp.unlink(missing_ok=True)
        return {"schedule": self.kind, "task": self.path, "removed": True}

    def _verbose(self) -> dict[str, str]:
        code, out = self._schtasks("/Query", "/TN", self.path, "/FO", "LIST", "/V")
        if code != 0:
            return {}
        row: dict[str, str] = {}
        for line in out.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() not in row:
                row[key.strip()] = value.strip()
        return row

    def running(self) -> bool:
        return self._verbose().get("Status", "").lower() == "running"

    def start(self, run: bool = True) -> tuple[bool, str]:
        """Enable, then run now unless `run` is false (a periodic job waits for its trigger, as
        launchd's RunAtLoad=false tick does)."""
        code, out = self._schtasks("/Change", "/TN", self.path, "/ENABLE")
        if not run:
            return code == 0, out
        code, out = self._schtasks("/Run", "/TN", self.path)
        return code == 0, out

    def stop(self) -> tuple[bool, str]:
        """End then disable, so it stays off across logons (DEC-0032 "Off")."""
        self._schtasks("/End", "/TN", self.path)
        code, out = self._schtasks("/Change", "/TN", self.path, "/DISABLE")
        return code == 0, out

    def restart(self) -> tuple[bool, str]:
        self._schtasks("/End", "/TN", self.path)
        return self.start()

    def status(self) -> dict:
        row = self._verbose()
        return {"kind": self.kind, "task": self.path, "installed": bool(row),
                "state": row.get("Status") or row.get("Scheduled Task State"),
                "last_run": row.get("Last Run Time"), "last_result": row.get("Last Result")}


# --- Linux: systemd user units ----------------------------------------------------------------

class SystemdJob:
    kind = "systemd"

    def __init__(self, job: Job, *, runner=subprocess.run, which=shutil.which):
        self.job, self.runner, self.which = job, runner, which
        self.name = f"project-observatory-{instance(job.workspace)}-{job.name}"
        root = os.environ.get("XDG_CONFIG_HOME")
        self.units = (Path(root) if root and Path(root).is_absolute() else Path.home() / ".config") / "systemd" / "user"

    @staticmethod
    def quote(value: str) -> str:
        # `%` is a systemd specifier and `$` expands in ExecStart: a path holding either would run
        # something other than it names, so it is refused rather than escaped.
        if any(c in value for c in '"\\\n%$'):
            raise ScheduleError("A path with a quote, backslash, newline, % or $ cannot go in a systemd unit")
        return f'"{value}"'

    @property
    def unit(self) -> str:
        return f"{self.name}.service"

    def service_text(self) -> str:
        j = self.job
        lines = ["[Unit]", f"Description=Project Observatory {j.name} for {j.workspace}", "",
                 "[Service]", f"Type={'simple' if j.service else 'oneshot'}",
                 "ExecStart=" + " ".join(self.quote(a) for a in j.argv),
                 f"WorkingDirectory={self.quote(str(j.cwd))}"]
        lines += [f"Environment={self.quote(f'{k}={v}')}" for k, v in j.env]
        lines += [f"StandardOutput=append:{j.stdout}", f"StandardError=append:{j.stderr}", "UMask=0077",
                  f"Nice={10 if j.background else 0}"]
        if j.service:
            lines += ["Restart=always", "RestartSec=10", f"TimeoutStopSec={max(20, j.time_limit_seconds or 20)}"]
        elif j.time_limit_seconds:
            lines += [f"TimeoutStartSec={j.time_limit_seconds}"]
        # A pass that starts an update in a session of its own must not lose it to the
        # control-group kill on a stop or a logout (maintenance, audit A03).
        lines += ["KillMode=process" if j.name == "maintain" else "KillMode=control-group", ""]
        if j.service:
            lines += ["[Install]", "WantedBy=default.target", ""]
        return "\n".join(lines)

    def timer_text(self) -> str | None:
        j = self.job
        if j.service:
            return None
        lines = ["[Unit]", f"Description=Run Project Observatory {j.name} for {j.workspace}", "", "[Timer]"]
        if j.at_login:
            lines.append(f"OnStartupSec={max(1, j.first_delay_seconds)}s")
        else:
            lines.append(f"OnActiveSec={max(60, j.interval_seconds or 60)}s")
        lines += [f"OnUnitActiveSec={j.interval_seconds}s", "Persistent=true", "", "[Install]", "WantedBy=timers.target", ""]
        return "\n".join(lines)

    def _systemctl(self, *args: str) -> tuple[int, str]:
        if self.which("systemctl") is None:
            return 127, "no systemd user manager (`systemctl --user`) on this machine"
        try:
            p = self.runner(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 125, type(exc).__name__
        return p.returncode, (p.stdout + p.stderr).strip()

    def _target(self) -> str:
        return self.unit if self.job.service else f"{self.name}.timer"

    def installed(self) -> bool:
        return (self.units / self._target()).is_file()

    def loaded(self) -> bool:
        return self.installed() and self._systemctl("is-enabled", self._target())[0] == 0

    def install(self) -> dict:
        if self.which("systemctl") is None:
            raise ScheduleError("no systemd user manager (`systemctl --user`) on this machine")
        self.units.mkdir(parents=True, exist_ok=True)
        changed = False
        files = [(self.unit, self.service_text())]
        if self.timer_text() is not None:
            files.append((f"{self.name}.timer", self.timer_text()))
        for name, text in files:
            target = self.units / name
            if not target.is_file() or target.read_text() != text:
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_text(text)
                os.replace(tmp, target)
                changed = True
        for args in (("daemon-reload",), ("enable", "--now", self._target())):
            code, out = self._systemctl(*args)
            if code != 0:
                raise ScheduleError(f"systemctl --user {' '.join(args)} failed: {out[:200]}")
        if changed and self.job.service:
            self._systemctl("restart", self.unit)
        return {"schedule": self.kind, "unit": self._target(), "changed": changed}

    def uninstall(self) -> dict:
        self._systemctl("disable", "--now", self._target())
        for suffix in (".service", ".timer"):
            (self.units / f"{self.name}{suffix}").unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        return {"schedule": self.kind, "unit": self._target(), "removed": True}

    def running(self) -> bool:
        return self._systemctl("is-active", self.unit)[0] == 0

    def start(self, run: bool = True) -> tuple[bool, str]:  # noqa: ARG002 - a timer waits for itself
        code, out = self._systemctl("enable", "--now", self._target())
        return code == 0, out

    def stop(self) -> tuple[bool, str]:
        code, out = self._systemctl("disable", "--now", self._target())
        return code == 0, out

    def restart(self) -> tuple[bool, str]:
        code, out = self._systemctl("restart", self.unit)
        return code == 0, out

    def status(self) -> dict:
        _, out = self._systemctl("show", "-p", "ActiveState,SubState,MainPID,UnitFileState", self._target())
        fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return {"kind": self.kind, "unit": self._target(), "installed": self.installed(),
                "state": fields.get("ActiveState"), "enabled": fields.get("UnitFileState")}


def handle(name: str, workspace: Path, **kwargs):
    """This system's supervisor for an installed job, by name: enough to ask whether it is
    installed and to stop, start or restart it (its definition is the installer's business)."""
    job = Job(name=name, workspace=Path(workspace), argv=(sys.executable,), env=(), cwd=Path(workspace),
              stdout=Path(workspace) / "x", stderr=Path(workspace) / "x",
              interval_seconds=None if name == "server" else 3600)
    return supervisor(job, **kwargs)


def supervisor(job: Job, **kwargs):
    """The job on this system's supervisor; macOS keeps its launchd modules."""
    if sys.platform == "win32":
        return TaskSchedulerJob(job, **kwargs)
    if sys.platform.startswith("linux"):
        return SystemdJob(job, **kwargs)
    raise ScheduleError("launchd jobs are installed by tools/install_launchd.py and tools/serverd.py")
