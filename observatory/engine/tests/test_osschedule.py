#!/usr/bin/env python3
"""`osschedule` puts the engine's background jobs under each system's supervisor (W4).

The definitions are checked on every OS: the Task Scheduler XML (per user, no stored password,
the triggers and limits each job needs) and the systemd units (DEC-0032's service rules). The
`RealTaskScheduler` cases run only on the Windows CI job: a throwaway task is created, run, read
back, stopped and deleted, and the wrapper's log proves the job ran with its environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import osschedule  # noqa: E402

NS = {"t": osschedule.TASK_NAMESPACE}


def job(name: str, workspace: Path, **kw) -> osschedule.Job:
    base = dict(name=name, workspace=workspace, argv=(sys.executable, str(ROOT / f"tools/{name}.py")),
                env=(("OBSERVATORY_HOME", str(workspace)), ("OBSERVATORY_SYSTEM_SETUP", "1")),
                cwd=ROOT, stdout=workspace / "store/logs" / f"{name}.log", stderr=workspace / "store/logs" / f"{name}.err")
    base.update(kw)
    return osschedule.Job(**base)


TICK = dict(interval_seconds=1800, time_limit_seconds=1700)
SERVER = dict(background=False)
MAINTAIN = dict(interval_seconds=3600, at_login=True, first_delay_seconds=90, time_limit_seconds=3000)


class FakeRunner:
    """Records schtasks/systemctl calls; answers each verb with the code the test sets."""

    def __init__(self, codes=None):
        self.calls, self.codes = [], codes or {}

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        verb = argv[1] if argv[0] == "schtasks" else argv[2]
        return subprocess.CompletedProcess(argv, self.codes.get(verb, 0), stdout="", stderr="")

    def verbs(self):
        return [c[1] if c[0] == "schtasks" else c[2] for c in self.calls]


class Workspace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)


class TaskXml(Workspace):
    def task(self, name, **kw):
        return osschedule.TaskSchedulerJob(job(name, self.ws, **kw), user="S-1-5-21-1-2-3-1001",
                                           which=lambda _: "schtasks")

    def tree(self, name, **kw):
        return ET.fromstring(self.task(name, **kw).xml().split("\n", 1)[1])

    def test_it_runs_as_this_account_without_a_password(self):
        doc = self.tree("tick", **TICK)
        principal = doc.find("t:Principals/t:Principal", NS)
        self.assertEqual(principal.find("t:LogonType", NS).text, "InteractiveToken")
        self.assertEqual(principal.find("t:RunLevel", NS).text, "LeastPrivilege")
        self.assertEqual(principal.find("t:UserId", NS).text, "S-1-5-21-1-2-3-1001")
        self.assertNotIn("Password", self.task("tick", **TICK).xml())

    def test_the_tick_repeats_and_has_a_ceiling(self):
        doc = self.tree("tick", **TICK)
        self.assertIsNone(doc.find("t:Triggers/t:LogonTrigger", NS))   # a logon is no reason to scan
        self.assertEqual(doc.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text, "PT30M")
        self.assertEqual(doc.find("t:Settings/t:ExecutionTimeLimit", NS).text, "PT28M20S")
        self.assertIsNone(doc.find("t:Settings/t:RestartOnFailure", NS))

    def test_the_server_starts_at_logon_restarts_and_has_no_limit(self):
        doc = self.tree("server", **SERVER)
        self.assertIsNotNone(doc.find("t:Triggers/t:LogonTrigger", NS))
        self.assertEqual(doc.find("t:Settings/t:RestartOnFailure/t:Interval", NS).text, "PT1M")
        self.assertEqual(doc.find("t:Settings/t:ExecutionTimeLimit", NS).text, "PT0S")
        self.assertEqual(doc.find("t:Settings/t:Priority", NS).text, "5")

    def test_maintenance_waits_after_logon_then_runs_hourly(self):
        doc = self.tree("maintain", **MAINTAIN)
        self.assertEqual(doc.find("t:Triggers/t:LogonTrigger/t:Delay", NS).text, "PT1M30S")
        self.assertEqual(doc.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text, "PT1H")

    def test_the_action_is_the_wrapper_with_the_jobs_environment(self):
        program, args = self.task("tick", **TICK).command()
        self.assertEqual(args[0], str(osschedule.WRAPPER))
        self.assertIn(f"OBSERVATORY_HOME={self.ws}", args)
        cut = args.index("--")
        self.assertEqual(args[cut + 1], sys.executable)
        self.assertEqual(args[cut + 2], str(ROOT / "tools/tick.py"))

    def test_pythonw_is_chosen_when_it_sits_beside_python(self):
        folder = self.ws / "Scripts"
        folder.mkdir()
        (folder / "python.exe").write_bytes(b"")
        self.assertEqual(osschedule.windowless(str(folder / "python.exe")), str(folder / "python.exe"))
        (folder / "pythonw.exe").write_bytes(b"")
        self.assertEqual(osschedule.windowless(str(folder / "python.exe")), str(folder / "pythonw.exe"))

    def test_each_workspace_has_its_own_task(self):
        other = self.ws / "other"
        other.mkdir()
        a = osschedule.TaskSchedulerJob(job("tick", self.ws, **TICK))
        b = osschedule.TaskSchedulerJob(job("tick", other, **TICK))
        self.assertNotEqual(a.path, b.path)
        self.assertTrue(a.path.startswith("\\ProjectObservatory\\") and a.path.endswith("-tick"))


class TaskLifecycle(Workspace):
    def task(self, runner):
        return osschedule.TaskSchedulerJob(job("tick", self.ws, **TICK), runner=runner, which=lambda _: "schtasks")

    def test_a_second_install_changes_nothing(self):
        runner = FakeRunner()
        self.assertTrue(self.task(runner).install()["changed"])
        runner.calls.clear()
        self.assertFalse(self.task(runner).install()["changed"])
        self.assertNotIn("/Create", runner.verbs())

    def test_a_refused_create_names_the_task(self):
        with self.assertRaises(osschedule.ScheduleError) as caught:
            self.task(FakeRunner({"/Create": 1})).install()
        self.assertIn("\\ProjectObservatory\\", str(caught.exception))

    def test_stop_stays_off_and_start_comes_back(self):
        runner = FakeRunner()
        self.task(runner).stop()
        self.assertEqual(runner.verbs(), ["/End", "/Change"])
        self.assertIn("/DISABLE", runner.calls[-1])
        runner.calls.clear()
        self.task(runner).start()
        self.assertEqual(runner.verbs(), ["/Change", "/Run"])
        self.assertIn("/ENABLE", runner.calls[0])

    def test_without_schtasks_it_says_so(self):
        task = osschedule.TaskSchedulerJob(job("tick", self.ws, **TICK), which=lambda _: None)
        with self.assertRaises(osschedule.ScheduleError):
            task.install()


class SystemdUnits(Workspace):
    def unit(self, name, **kw):
        return osschedule.SystemdJob(job(name, self.ws, **kw), which=lambda _: "systemctl")

    def test_the_server_is_a_restarting_service_started_at_login(self):
        text = self.unit("server", **SERVER).service_text()
        for line in ("Type=simple", "Restart=always", "RestartSec=10", "WantedBy=default.target", "UMask=0077"):
            self.assertIn(line, text)
        self.assertIsNone(self.unit("server", **SERVER).timer_text())

    def test_the_tick_is_a_oneshot_on_a_timer(self):
        u = self.unit("tick", **TICK)
        self.assertIn("Type=oneshot", u.service_text())
        self.assertIn("OnUnitActiveSec=1800s", u.timer_text())
        self.assertIn("OnActiveSec=1800s", u.timer_text())
        self.assertNotIn("OnStartupSec", u.timer_text())

    def test_maintenance_keeps_the_update_it_starts(self):
        u = self.unit("maintain", **MAINTAIN)
        self.assertIn("KillMode=process", u.service_text())
        self.assertIn("OnStartupSec=90s", u.timer_text())

    def test_a_path_systemd_would_reinterpret_is_refused(self):
        with self.assertRaises(osschedule.ScheduleError):
            self.unit("tick", **TICK, cwd=Path("/tmp/100%"), ).service_text()

    def test_install_enables_the_right_unit(self):
        os.environ["XDG_CONFIG_HOME"] = str(self.ws / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        runner = FakeRunner()
        osschedule.SystemdJob(job("tick", self.ws, **TICK), runner=runner, which=lambda _: "systemctl").install()
        self.assertIn(["systemctl", "--user", "enable", "--now",
                       f"project-observatory-{osschedule.instance(self.ws)}-tick.timer"], runner.calls)


class Definitions(Workspace):
    def test_a_secret_shaped_environment_entry_cannot_be_written(self):
        with self.assertRaises(ValueError):
            job("tick", self.ws, **TICK, env=(("A", "x\ny"),))

    def test_a_periodic_job_runs_at_most_once_a_minute(self):
        with self.assertRaises(ValueError):
            job("tick", self.ws, interval_seconds=30)


class Wrapper(Workspace):
    def test_the_job_gets_its_environment_folder_and_logs(self):
        out, err = self.ws / "logs/job.log", self.ws / "logs/job.err"
        script = "import os,sys; print(os.environ['PROBE'], os.getcwd()); sys.stderr.write('e\\n'); sys.exit(3)"
        code = subprocess.run([sys.executable, str(ROOT / "tools/scheduled.py"), "--cwd", str(self.ws),
                               "--stdout", str(out), "--stderr", str(err), "--env", "PROBE=here",
                               "--", sys.executable, "-c", script]).returncode
        self.assertEqual(code, 3)
        text = out.read_bytes().decode()
        self.assertIn("here", text)
        self.assertIn(str(self.ws.resolve()).lower()[-12:], text.lower())
        self.assertEqual(err.read_bytes().replace(b"\r\n", b"\n"), b"e\n")


@unittest.skipUnless(os.name == "nt", "needs Windows Task Scheduler")
class RealTaskScheduler(Workspace):
    def test_a_task_is_created_run_read_stopped_and_deleted(self):
        marker = self.ws / "ran.txt"
        script = self.ws / "probe.py"
        script.write_text("import os, pathlib\npathlib.Path(os.environ['MARK']).write_text('ok')\n")
        definition = osschedule.Job(name="tick", workspace=self.ws, argv=(sys.executable, str(script)),
                                    env=(("MARK", str(marker)),), cwd=self.ws,
                                    stdout=self.ws / "probe.log", stderr=self.ws / "probe.err",
                                    interval_seconds=3600, time_limit_seconds=600)
        task = osschedule.TaskSchedulerJob(definition)
        self.addCleanup(task.uninstall)
        self.assertTrue(task.install()["changed"], task.xml())
        self.assertTrue(task.installed())
        self.assertFalse(task.install()["changed"])
        ok, out = task.start()
        self.assertTrue(ok, out)
        for _ in range(60):
            if marker.exists():
                break
            time.sleep(1)
        self.assertEqual(marker.read_text() if marker.exists() else "",
                         "ok", (self.ws / "probe.err").read_text() if (self.ws / "probe.err").exists() else task.status())
        self.assertTrue(task.status()["installed"])
        self.assertTrue(task.stop()[0])
        task.uninstall()
        self.assertFalse(task.installed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
