#!/usr/bin/env python3
"""Updates that arrive by themselves, and data that survives a reinstall.

docs/runs/2026-10-05-auto-update, R1–R10. Every test runs in a synthetic workspace with
the backups root in a temporary directory. launchd, systemd, the Keychain, Gatekeeper and
GitHub are fakes, so no test schedules a job, writes a credential or reaches the network.
"""
from __future__ import annotations
import contextlib
import datetime
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration as config
import workspace
import workspace_upgrade
import backup_vault as vault
import maintenance as M
import app_update as A

AT = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.timezone.utc)
SECRET_MARK = "zebra-orchard-" + "5521"


class FakeStore:
    """The OS credential store: a dict, and a log of what it was asked."""

    def __init__(self, fail=False):
        self.items, self.fail, self.calls = {}, fail, []

    def kind(self):
        return "fake"

    def put(self, label, value):
        self.calls.append(("put", label))
        if self.fail:
            raise vault.BackupError("the fake store refused")
        self.items[label] = value
        return "fake"

    def get(self, label):
        self.calls.append(("get", label))
        return self.items.get(label)


class FakeCommands:
    """`full update --check/--apply`, answered from a script of exit codes."""

    def __init__(self, check=0, apply=0, latest="9.9.9"):
        self.check, self.apply, self.latest, self.calls = check, apply, latest, []

    def full(self, *args, timeout=0, env=None):
        self.calls.append((args, dict(env or {})))
        if args[-1] == "--check":
            return self.check, {"target": self.latest}, "" if self.check in (0, 10) else "network down"
        return self.apply, {"version": self.latest}, "" if self.apply == 0 else "synthetic failure"


class FakeServices:
    def __init__(self, loaded=("tick", "server"), start_ok=True):
        self.loaded_names, self.start_ok, self.calls = set(loaded), start_ok, []

    def managed(self):
        return [{"name": n, "label": f"label.{n}", "plist": f"/synthetic/{n}.plist"} for n in ("tick", "server")]

    def loaded(self, label):
        return label.split(".")[-1] in self.loaded_names

    def stop(self, label):
        self.calls.append(("stop", label.split(".")[-1]))
        return True, ""

    def start(self, plist):
        self.calls.append(("start", Path(plist).stem))
        return self.start_ok, "" if self.start_ok else "synthetic bootstrap error"


class FakeSchedule:
    kind = "fake"

    def __init__(self, fail=None):
        self.fail, self.installs, self.removed = fail, 0, 0

    def install(self):
        if self.fail:
            raise config.ConfigurationError(self.fail)
        self.installs += 1
        return {"schedule": "fake", "label": "synthetic.maintain", "changed": True}

    def uninstall(self):
        self.removed += 1
        return {"schedule": "fake", "removed": True}

    def status(self):
        return {"kind": "fake", "installed": self.installs > 0, "loaded": self.installs > 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "private workspace"
        self.root = self.base / "cloud" / "Backups"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home),
                                           "OBSERVATORY_BACKUPS": str(self.root),
                                           "HOME": str(self.base / "user"),
                                           "XDG_CONFIG_HOME": str(self.base / "xdg-config")}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        from store import db
        importlib.reload(db)
        conn = db.connect()
        conn.execute("INSERT INTO events(id,kind,occurred_at,actor) VALUES ('synthetic-event','session','2026-01-01T00:00:00Z','fixture')")
        conn.commit()
        conn.close()
        (self.home / "secrets" / "demo-slot").write_text(SECRET_MARK)
        self.store = FakeStore()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workspace.main(list(argv))
        text = out.getvalue()
        try:
            return code, json.loads(text) if text.strip() else {}, err.getvalue()
        except ValueError:
            return code, {"raw": text}, err.getvalue()


# --- R1: the switch ------------------------------------------------------------------

class Switch(Base):
    def test_automatic_updates_are_on_until_a_person_turns_them_off(self):
        self.assertTrue(M.auto_enabled(self.home))
        self.assertTrue(M.schedule_wanted(self.home))
        code, doc, _ = self.cli("auto-update", "off")
        self.assertEqual((code, doc["auto_update"]), (0, False))
        self.assertIs(config.load(self.home)["updates"]["auto"], False)
        code, doc, _ = self.cli("auto-update", "on")
        self.assertEqual((code, doc["auto_update"]), (0, True))
        code, doc, _ = self.cli("auto-update", "status")
        self.assertEqual(code, 0)
        self.assertIs(doc["auto_update"], True)
        self.assertIn("warnings", doc)

    def test_the_setting_is_validated_and_stays_optional(self):
        doc = config.load(self.home)
        for bad in ({"auto": "yes"}, {"sometimes": True}, ["auto"]):
            doc["updates"] = bad
            workspace.write_json(self.home / "config/settings.json", doc)
            with self.assertRaises(config.ConfigurationError):
                config.load(self.home)
        doc["updates"] = {"auto": False, "scheduled": True}
        workspace.write_json(self.home / "config/settings.json", doc)
        self.assertEqual(config.load(self.home)["updates"], {"auto": False, "scheduled": True})
        # An older reader reads only the keys it knows and `must_understand` stays empty.
        self.assertEqual(config.load(self.home).get("must_understand", []), [])


# --- R5: the passphrase outside the workspace ------------------------------------------

class Passphrase(Base):
    def test_one_is_generated_by_default_and_kept_outside(self):
        self.assertIsNone(vault.passphrase(self.home))
        out = vault.ensure_passphrase(self.home, self.store)
        value = vault.passphrase(self.home)
        self.assertEqual((out["passphrase"], out["kept_outside"]), ("generated", "fake"))
        self.assertGreaterEqual(len(value), 40)
        self.assertEqual(self.store.items[out["label"]], value)
        self.assertEqual(stat.S_IMODE(vault.passphrase_file(self.home).stat().st_mode), 0o600)
        self.assertNotIn(value, json.dumps(out))
        # Idempotent: a second call neither regenerates nor rewrites the store.
        puts = [c for c in self.store.calls if c[0] == "put"]
        again = vault.ensure_passphrase(self.home, self.store)
        self.assertEqual((again["passphrase"], vault.passphrase(self.home)), ("configured", value))
        self.assertEqual([c for c in self.store.calls if c[0] == "put"], puts)

    def test_a_persons_passphrase_is_mirrored_and_an_environment_one_is_not(self):
        vault.set_passphrase(self.home, "a person's own phrase, with spaces")
        out = vault.ensure_passphrase(self.home, self.store)
        self.assertEqual(out["passphrase"], "configured")
        self.assertEqual(self.store.items[out["label"]], "a person's own phrase, with spaces")
        other = FakeStore()
        with patch.dict(os.environ, {vault.PASS_ENV: "from the environment only"}):
            self.assertEqual(vault.ensure_passphrase(self.home, other)["passphrase"], "environment")
        self.assertEqual(other.items, {})

    def test_a_store_that_refuses_is_named_and_the_backup_still_works(self):
        out = vault.ensure_passphrase(self.home, FakeStore(fail=True))
        self.assertEqual(out["passphrase"], "generated")
        self.assertIsNone(out["kept_outside"])
        self.assertIn("only in", out["warning"])
        self.assertIsNotNone(vault.passphrase(self.home))

    def runner(self, answers):
        calls = []

        def run(argv, input=None, **kw):
            calls.append((list(argv), input))
            code, out = answers.pop(0) if answers else (0, "")
            return subprocess.CompletedProcess(argv, code, out, "")
        return run, calls

    def test_keychain_values_travel_on_stdin_hex_encoded(self):
        value = 'quote " backslash \\ and spaces'
        run, calls = self.runner([(0, ""), (0, "hex:" + value.encode().hex() + "\n")])
        store = vault.SecretStore(runner=run, platform="darwin", which=lambda name: "/usr/bin/" + name)
        self.assertEqual(store.put("private workspace-1234abcd", value), "keychain")
        self.assertEqual(store.get("private workspace-1234abcd"), value)
        for argv, data in calls:
            self.assertFalse(any(value in a or value.encode().hex() in a for a in argv), argv)
        self.assertEqual(calls[0][0], ["security", "-i"])
        self.assertIn("hex:" + value.encode().hex(), calls[0][1])
        self.assertIn("-U", calls[0][1])

    def test_secret_service_values_travel_on_stdin(self):
        run, calls = self.runner([(0, ""), (0, "the value\n")])
        store = vault.SecretStore(runner=run, platform="linux",
                                  which=lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None)
        self.assertEqual(store.put("ws-1", "the value"), "secret-service")
        self.assertEqual(store.get("ws-1"), "the value")
        self.assertEqual(calls[0][1], "the value")
        self.assertNotIn("the value", " ".join(calls[0][0]))

    def test_a_secret_service_that_does_not_answer_falls_back_to_the_file(self):
        # Review F7: `secret-tool` installed, no keyring running.
        run, calls = self.runner([(1, ""), (1, "")])
        store = vault.SecretStore(runner=run, platform="linux",
                                  which=lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None)
        self.assertEqual(store.put("ws-1", "kept anyway"), "file")
        self.assertEqual(store.get("ws-1"), "kept anyway")

    def test_the_file_store_is_owner_only_outside_the_workspace(self):
        store = vault.SecretStore(platform="linux", which=lambda name: None)
        self.assertEqual(store.put("ws-1", "file value"), "file")
        file = self.base / "xdg-config" / "project-observatory" / "backup-passphrases" / "ws-1"
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(file.parent.stat().st_mode), 0o700)
        self.assertNotIn(self.home, file.parents)
        self.assertEqual(store.get("ws-1"), "file value")
        file.chmod(0o644)
        self.assertIsNone(store.get("ws-1"), "a copy others can read is not trusted")

    def test_a_label_that_could_break_the_command_is_refused(self):
        store = vault.SecretStore(platform="linux", which=lambda name: None)
        for bad in ('ws"; rm', "ws\nx", ""):
            with self.assertRaises(vault.BackupError):
                store.put(bad, "v")


# --- R2/R7: one pass -----------------------------------------------------------------

class Pass(Base):
    def run_pass(self, commands, at=AT, services=None, app=None):
        return M.run_pass(self.home, at=at, commands=commands, services=services, store=self.store,
                          app=app or NoApp(), sleep=lambda s: None)

    def test_codes_match_the_updater(self):
        import engine_update as eu
        self.assertEqual((M.UPDATE_OK, M.UPDATE_FAILED, M.UPDATE_REFUSED, M.UPDATE_UNDETERMINED,
                          M.UPDATE_NEEDS_PERSON, M.UPDATE_SERVICES, M.UPDATE_AVAILABLE),
                         (eu.EXIT_OK, eu.EXIT_FAILED, eu.EXIT_REFUSED, eu.EXIT_UNDETERMINED,
                          eu.EXIT_NEEDS_PERSON, eu.EXIT_SERVICES, eu.EXIT_UPDATE_AVAILABLE))

    def test_up_to_date_checks_once_a_day_and_takes_the_daily_backup(self):
        commands = FakeCommands(check=0)
        report = self.run_pass(commands, services=FakeServices())
        self.assertEqual(report["update"]["result"], "up-to-date")
        self.assertEqual(report["snapshot"]["result"], "taken")
        self.assertTrue(report["snapshot"]["encrypted"], "a generated passphrase encrypts the copy")
        self.assertEqual(len(list(vault.root_info(self.home)["path"].glob("daily-*.obsnap"))), 1)
        self.assertEqual(report["passphrase"]["passphrase"], "generated")
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=2))
        self.assertEqual((later["update"]["result"], later["snapshot"]["result"]), ("not-due", "not-due"))
        self.assertEqual(len(commands.calls), 1)
        next_day = self.run_pass(commands, at=AT + datetime.timedelta(hours=25), services=FakeServices())
        self.assertEqual(next_day["update"]["result"], "up-to-date")
        self.assertEqual(len(commands.calls), 2)

    def test_a_newer_release_is_installed_with_the_system_flag(self):
        commands = FakeCommands(check=10, apply=0, latest="9.9.9")
        report = self.run_pass(commands)
        self.assertEqual(report["update"]["result"], "updated")
        self.assertEqual([c[0] for c in commands.calls], [("update", "--check"), ("update", "--apply")])
        self.assertEqual(commands.calls[1][1].get("OBSERVATORY_SYSTEM_SETUP"), "1")
        state = M.read_state(self.home)
        self.assertEqual((state["update"]["from"], state["update"]["to"]), (config.VERSION, "9.9.9"))
        self.assertEqual(state["snapshot"]["result"], "before-upgrade", "the update's own snapshot counts")
        self.assertNotIn("app", report, "the new release's code handles the app on the next pass")

    def test_a_failed_update_is_retried_the_next_day_not_the_next_hour(self):
        commands = FakeCommands(check=10, apply=1)
        self.assertEqual(self.run_pass(commands)["update"]["result"], "failed-rolled-back")
        self.assertEqual(M.read_state(self.home)["update"]["failures"], 1)
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=1))["update"]["result"], "not-due")
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=24))["update"]["result"],
                         "failed-rolled-back")
        self.assertEqual(M.read_state(self.home)["update"]["failures"], 2)
        status = M.status(self.home, schedule=FakeSchedule())
        self.assertTrue(any("failed" in w for w in status["warnings"]))

    def test_an_update_that_needs_a_person_stops_the_attempts(self):
        commands = FakeCommands(check=10, apply=4)
        self.assertEqual(self.run_pass(commands)["update"]["result"], "needs-person")
        later = self.run_pass(commands, at=AT + datetime.timedelta(days=3))
        self.assertEqual(later["update"]["result"], "waiting-for-person")
        self.assertEqual(len(commands.calls), 2)
        self.assertTrue(any("needs a person" in w for w in M.status(self.home, schedule=FakeSchedule())["warnings"]))

    def test_no_network_is_undetermined_and_never_up_to_date(self):
        report = self.run_pass(FakeCommands(check=3))
        self.assertEqual(report["update"]["result"], "undetermined")
        self.assertEqual(M.read_state(self.home)["check"]["detail"], "network down")

    def test_turned_off_means_no_check_and_the_backup_still_runs(self):
        M.set_setting(self.home, "auto", False)
        commands = FakeCommands(check=10)
        report = self.run_pass(commands, services=FakeServices())
        self.assertEqual(report["update"]["result"], "off")
        self.assertEqual(commands.calls, [])
        self.assertEqual(report["snapshot"]["result"], "taken")

    def test_the_backup_stops_and_restarts_only_loaded_jobs(self):
        services = FakeServices(loaded=("server",))
        self.run_pass(FakeCommands(), services=services)
        self.assertEqual(services.calls, [("stop", "server"), ("start", "server")])

    def test_a_backup_that_keeps_failing_is_recorded_and_the_jobs_come_back(self):
        services = FakeServices()
        with patch.object(workspace_upgrade, "snapshot",
                          side_effect=config.ConfigurationError("Workspace changed during snapshot")):
            report = self.run_pass(FakeCommands(), services=services)
        self.assertEqual(report["snapshot"]["result"], "failed")
        self.assertIn("changed during snapshot", M.read_state(self.home)["snapshot_failure"]["detail"])
        self.assertEqual(sorted(c for c in services.calls if c[0] == "start"),
                         [("start", "server"), ("start", "tick")])
        self.assertTrue(any("no full backup" in w for w in M.status(self.home, schedule=FakeSchedule())["warnings"]))

    def test_a_job_that_does_not_restart_is_recorded(self):
        self.run_pass(FakeCommands(), services=FakeServices(start_ok=False))
        self.assertEqual(len(M.read_state(self.home)["services_not_restarted"]), 2)

    def test_a_person_who_updated_lets_the_automatic_attempts_resume(self):
        # Review F3: needs-person is about the engine that needed the person.
        state = {"update": {"result": "needs-person", "from": "0.0.1", "at": "2026-10-01T00:00:00Z"}}
        M.write_state(self.home, state)
        report = self.run_pass(FakeCommands(check=0))
        self.assertEqual(report["update"]["result"], "up-to-date")
        self.assertEqual(M.read_state(self.home)["update"]["result"], "resolved-by-person")

    def test_an_update_already_running_is_never_joined(self):
        import engine_update
        with engine_update.update_lock(self.home):
            commands = FakeCommands(check=10)
            self.assertEqual(self.run_pass(commands)["update"]["result"], "another-update-running")
        self.assertEqual([c[0] for c in commands.calls], [("update", "--check")])

    def test_services_an_interrupted_pass_stopped_are_started_by_the_next(self):
        # Review F2: the list is persisted before the stop.
        services = FakeServices(loaded=())
        M.write_state(self.home, {"stopped_services": [
            {"name": "tick", "label": "label.tick", "plist": "/synthetic/tick.plist"}]})
        report = self.run_pass(FakeCommands(), services=services, at=AT)
        self.assertEqual(report["restarted_after_interruption"], [{"service": "tick", "started": True, "detail": ""}])
        self.assertNotIn("stopped_services", M.read_state(self.home))

    def test_a_running_tick_defers_the_backup_instead_of_killing_it(self):
        services = FakeServices()
        with patch.object(M, "_tick_busy", return_value=True):
            report = self.run_pass(FakeCommands(), services=services)
        self.assertEqual(report["snapshot"]["result"], "deferred")
        self.assertEqual(services.calls, [], "nothing stopped")
        self.assertEqual(self.run_pass(FakeCommands(), services=services, at=AT + datetime.timedelta(hours=1))
                         ["snapshot"]["result"], "taken", "the next hour tries again")

    def test_daily_backups_never_rotate_away_a_persons_snapshot(self):
        vault.ensure_passphrase(self.home, self.store)
        workspace_upgrade.snapshot(self.home, writers_stopped=True)  # a person's own
        root = vault.root_info(self.home)["path"]
        for day in range(5):
            self.run_pass(FakeCommands(), services=FakeServices(), at=AT + datetime.timedelta(days=day))
        self.assertEqual(len(list(root.glob("snapshot-*.obsnap"))), 1)
        self.assertEqual(len(list(root.glob("daily-*.obsnap"))), vault.KEEP)

    def test_a_second_pass_finds_the_first_busy(self):
        with M.pass_lock(self.home) as held:
            self.assertTrue(held)
            self.assertEqual(self.run_pass(FakeCommands())["status"], "busy")

    def test_the_state_file_is_owner_only_and_holds_no_secret(self):
        self.run_pass(FakeCommands(), services=FakeServices())
        file = M.state_path(self.home)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        text = file.read_text()
        self.assertNotIn(vault.passphrase(self.home), text)
        self.assertNotIn(SECRET_MARK, text)


class NoApp:
    def step(self, state, at):
        return {"result": "not-installed"}


# --- R3: the schedule -------------------------------------------------------------------

class Schedule(Base):
    def test_the_launchd_job_runs_hourly_and_is_never_a_managed_job(self):
        sched = M.LaunchdSchedule(self.home)
        doc = sched.document()
        self.assertTrue(doc["Label"].endswith(".maintain"))
        self.assertEqual(doc["ProgramArguments"][1:], [str(ROOT / "tools" / "maintain.py"), "run"])
        self.assertEqual((doc["StartInterval"], doc["RunAtLoad"]), (3600, True))
        self.assertEqual(doc["EnvironmentVariables"]["OBSERVATORY_HOME"], str(self.home))
        self.assertEqual(doc["EnvironmentVariables"]["OBSERVATORY_SYSTEM_SETUP"], "1")
        self.assertEqual(sched.launch.lint_plist(doc), [])
        allowed = {"PATH", "OBSERVATORY_PYTHON", "HOME", "OBSERVATORY_HOME", "OBSERVATORY_SYSTEM_SETUP",
                   "OBSERVATORY_MCP_PROBE_TIMEOUT"}
        self.assertLessEqual(set(doc["EnvironmentVariables"]), allowed, "a plist is world-readable: no key")
        labels = {j["label"] for j in sched.launch.managed_jobs()}
        self.assertNotIn(doc["Label"], labels, "`full update` stops managed jobs; it would stop itself")

    def launchd(self):
        sched = M.LaunchdSchedule(self.home)
        self.loaded, self.stops = {"value": False}, []

        def start(plist):
            self.loaded["value"] = True
            return True, ""

        def stop(label):
            self.stops.append(label)
            self.loaded["value"] = False
            return True, ""
        for name, fn in (("job_loaded", lambda label: self.loaded["value"]), ("stop_job", stop), ("start_job", start)):
            patcher = patch.object(sched.launch, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)
        return sched

    def test_the_launchd_job_is_written_owner_only_and_loaded(self):
        sched = self.launchd()
        out = sched.install()
        self.assertTrue(out["changed"])
        self.assertEqual(stat.S_IMODE(sched.plist.stat().st_mode), 0o600)
        self.assertEqual(plistlib.loads(sched.plist.read_bytes())["Label"], sched.label)
        self.assertFalse(sched.install()["changed"], "an identical, loaded job is left as it is")
        sched.uninstall()
        self.assertFalse(sched.plist.exists())

    def test_a_changed_job_is_never_booted_out_from_inside_a_pass(self):
        # Review F1: the update a pass runs asks for the schedule; booting the job out
        # would kill that very pass. The definition is written and the reload deferred.
        sched = self.launchd()
        sched.install()
        doc = plistlib.loads(sched.plist.read_bytes())
        doc["Nice"] = 1  # an older definition on disk
        sched.plist.write_bytes(plistlib.dumps(doc))
        with patch.dict(os.environ, {M.IN_PASS_ENV: "1"}):
            out = sched.install()
        self.assertEqual(out.get("reload"), "deferred")
        self.assertEqual(self.stops, [], "never a bootout from inside a pass")
        self.assertTrue((self.home / "store/maintenance-reload-pending").exists())
        out = sched.install()  # outside a pass: the pending definition is loaded now
        self.assertTrue(out["changed"])
        self.assertEqual(len(self.stops), 1)
        self.assertFalse((self.home / "store/maintenance-reload-pending").exists())

    def test_a_pass_holding_its_lock_also_defers_a_reload(self):
        sched = self.launchd()
        sched.install()
        doc = plistlib.loads(sched.plist.read_bytes())
        doc["Nice"] = 1
        sched.plist.write_bytes(plistlib.dumps(doc))
        with M.pass_lock(self.home) as held:
            self.assertTrue(held)
            self.assertEqual(sched.install().get("reload"), "deferred")
        self.assertEqual(self.stops, [])

    def test_the_installed_path_is_kept_whoever_runs_the_installer(self):
        sched = self.launchd()
        sched.install()
        first = plistlib.loads(sched.plist.read_bytes())["EnvironmentVariables"]["PATH"]
        # A caller whose PATH holds another tool directory (launch_path keeps only those).
        tools = self.base / "another-caller-bin"
        tools.mkdir(mode=0o755)
        (tools / "git").write_text("#!/bin/sh\n")
        (tools / "git").chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ.get("PATH", "/usr/bin")}):
            self.assertIn(str(tools), sched.launch.launch_path(), "the fixture must change a fresh PATH")
            self.assertFalse(sched.install()["changed"], "a caller's PATH does not flip the job")
        self.assertEqual(plistlib.loads(sched.plist.read_bytes())["EnvironmentVariables"]["PATH"], first)

    def test_the_systemd_timer_is_hourly_persistent_and_quoted(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        sched = M.SystemdSchedule(self.home, runner=run, which=lambda name: "/usr/bin/systemctl")
        out = sched.install()
        service = (sched.units / f"{sched.name}.service").read_text()
        timer = (sched.units / f"{sched.name}.timer").read_text()
        self.assertIn(f'ExecStart="', service)
        self.assertIn('maintain.py" run', service)
        self.assertIn(f'Environment="OBSERVATORY_HOME={self.home}"', service)
        self.assertIn("OnUnitActiveSec=60min", timer)
        self.assertIn("Persistent=true", timer)
        self.assertEqual(calls, [["systemctl", "--user", "daemon-reload"],
                                 ["systemctl", "--user", "enable", "--now", f"{sched.name}.timer"]])
        self.assertTrue(out["changed"])
        for bad in ('a "quoted" path', "/opt/100%/x", "/opt/$HOME/x"):
            with self.assertRaises(config.ConfigurationError):
                sched.quote(bad)
        none = M.SystemdSchedule(self.home, runner=run, which=lambda name: None)
        with self.assertRaises(config.ConfigurationError):
            none.install()

    def test_ensure_schedules_and_respects_a_persons_uninstall(self):
        sched = FakeSchedule()
        out = M.ensure(self.home, schedule=sched, store=self.store)
        self.assertEqual((sched.installs, out["auto_update"]), (1, True))
        M.unschedule(self.home, schedule=sched)
        out = M.ensure(self.home, schedule=sched, store=self.store)
        self.assertEqual((out["schedule"]["result"], sched.installs), ("off", 1))
        M.ensure(self.home, schedule=sched, store=self.store, explicit=True)
        self.assertEqual(sched.installs, 2)
        self.assertTrue(M.schedule_wanted(self.home))

    def test_no_scheduler_falls_back_to_the_plugin_hook(self):
        with patch.object(M, "schedule_for", return_value=FakeSchedule(fail="no systemd user manager")), \
                patch.object(backup_vault_store(), "SecretStore", return_value=self.store), \
                patch.object(M, "run_pass", return_value={"status": "ran"}) as run:
            out = M.hook(self.home)
        self.assertEqual(out["schedule"]["result"], "unscheduled")
        self.assertIn("plugin", out["schedule"]["fallback"])
        self.assertEqual(out["pass"], {"status": "ran"})
        run.assert_called_once()

    def test_system_setup_needs_a_person_or_the_flag_and_never_a_temporary_home(self):
        with patch.object(M.sys, "stdin") as stdin:
            stdin.isatty.return_value = True
            self.assertFalse(M.system_setup_allowed(self.home), "a temporary home is a test's")
            real = Path("/opt/synthetic-observatory-home")  # outside every temporary directory
            with patch.object(M.tempfile, "gettempdir", return_value=str(self.base / "elsewhere")):
                self.assertTrue(M.system_setup_allowed(real))
                stdin.isatty.return_value = False
                self.assertFalse(M.system_setup_allowed(real))
        with patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "1"}):
            self.assertTrue(M.system_setup_allowed(self.home))
        with patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "0"}):
            self.assertFalse(M.system_setup_allowed(Path("/opt/synthetic-observatory-home")))

    def test_init_here_says_how_scheduling_will_happen_and_touches_nothing(self):
        fresh = self.base / "another home"
        with patch.dict(os.environ, {"OBSERVATORY_HOME": str(fresh)}), \
                patch.object(M, "schedule_for", side_effect=AssertionError("init must not schedule here")):
            code, doc, _ = self.cli("init")
        self.assertEqual(code, 0)
        self.assertEqual(doc["maintenance"]["result"], "not-scheduled-here")

    def test_an_update_keeps_a_schedule_a_person_removed(self):
        # Review F4: `full update` runs `maintain ensure --if-wanted`.
        sched = FakeSchedule()
        M.unschedule(self.home, schedule=sched)
        with patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "1"}), \
                patch.object(M, "schedule_for", return_value=sched), \
                patch.object(vault, "SecretStore", return_value=self.store):
            code, doc, _ = self.cli("maintain", "ensure", "--if-wanted")
            self.assertEqual((code, doc["schedule"]["result"]), (0, "off"))
            self.assertFalse(M.schedule_wanted(self.home))
            code, doc, _ = self.cli("maintain", "ensure")
        self.assertEqual(code, 0)
        self.assertTrue(M.schedule_wanted(self.home), "a person's own ensure turns it back on")
        self.assertEqual(sched.installs, 1)

    def test_maintain_ensure_refuses_without_a_person_or_the_flag(self):
        with patch.object(M, "schedule_for", side_effect=AssertionError("must not schedule")):
            code, _, err = self.cli("maintain", "ensure")
        self.assertEqual(code, 2)
        self.assertIn("OBSERVATORY_SYSTEM_SETUP=1", err)

    def test_doctor_names_an_unscheduled_job(self):
        with patch.object(M, "schedule_for", return_value=FakeSchedule()):
            report = workspace.doctor(self.home)["maintenance"]
        self.assertTrue(report["auto_update"])
        self.assertTrue(any("not scheduled" in w for w in report["warnings"]))


def backup_vault_store():
    return vault


# --- R8: a reinstall restores ----------------------------------------------------------

class Reinstall(Base):
    def backed_up(self):
        vault.ensure_passphrase(self.home, self.store)
        workspace_upgrade.snapshot(self.home, writers_stopped=True)
        files = sorted(vault.root_info(self.home)["path"].glob("snapshot-*.obsnap"))
        self.assertEqual(len(files), 1)
        return files[0]

    def test_the_newest_backup_of_this_path_comes_back_after_the_workspace_is_deleted(self):
        self.backed_up()
        old_label = vault._workspace_label(self.home)
        shutil.rmtree(self.home)
        out = M.restore_latest(self.home, store=self.store)
        self.assertEqual(out["status"], "restored")
        self.assertEqual((self.home / "secrets" / "demo-slot").read_text(), SECRET_MARK)
        self.assertEqual(vault._workspace_label(self.home), old_label, "the same backups folder continues")
        from store import db
        importlib.reload(db)
        conn = db.connect()
        self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE id='synthetic-event'").fetchone()[0], 1)
        conn.close()

    def test_init_restores_when_it_may_touch_the_machine_and_fresh_skips(self):
        self.backed_up()
        shutil.rmtree(self.home)
        with patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "1"}), \
                patch.object(vault, "SecretStore", return_value=self.store), \
                patch.object(M, "schedule_for", return_value=FakeSchedule()):
            code, doc, _ = self.cli("init")
            self.assertEqual((code, doc["status"]), (0, "restored"), doc)
            self.assertEqual((self.home / "secrets" / "demo-slot").read_text(), SECRET_MARK)
            self.assertIn("schedule", doc["maintenance"])
            shutil.rmtree(self.home)
            code, doc, _ = self.cli("init", "--fresh")
        self.assertEqual((code, doc["status"]), (0, "initialized"))
        self.assertFalse((self.home / "secrets" / "demo-slot").exists())

    def test_without_the_passphrase_init_names_the_backup_and_the_command(self):
        newest = self.backed_up()
        shutil.rmtree(self.home)
        with patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "1"}), \
                patch.object(vault, "SecretStore", return_value=FakeStore()), \
                patch.object(M, "schedule_for", return_value=FakeSchedule()):
            code, doc, _ = self.cli("init")
        self.assertEqual((code, doc["status"]), (0, "initialized"))
        self.assertEqual(doc["backups_found"]["newest"], str(newest))
        self.assertIn("full restore", doc["backups_found"]["next"])

    def test_restore_latest_by_hand_and_its_refusals(self):
        self.backed_up()
        with self.assertRaisesRegex(config.ConfigurationError, "empty destination"):
            M.restore_latest(self.home, store=self.store)
        shutil.rmtree(self.home)
        with patch.object(vault, "SecretStore", return_value=self.store):
            code, doc, _ = self.cli("restore", "--latest")
        self.assertEqual((code, doc["status"]), (0, "restored"))
        code, _, err = self.cli("restore")
        self.assertEqual(code, 2)
        self.assertIn("--latest", err)

    def test_another_workspace_with_a_longer_name_or_another_path_is_never_restored(self):
        # Review F5: `private workspace-*` must not match `private workspace-dev-…`, and the
        # same name at another path is another workspace.
        self.backed_up()
        root = vault.root_info(self.home)["path"].parent
        own = vault.root_info(self.home)["path"]
        lookalike = root / "private workspace-dev-9f8e7d6c"
        shutil.copytree(own, lookalike)
        (lookalike / vault.OWNER_FILE).unlink(missing_ok=True)
        elsewhere = root / "private workspace-0badc0de"
        shutil.copytree(own, elsewhere)
        (elsewhere / vault.OWNER_FILE).write_text("/somewhere/else/private workspace\n")
        labels = {c["label"] for c in vault.candidates(self.home)}
        self.assertEqual(labels, {own.name})

    def test_a_damaged_newest_backup_falls_back_to_an_older_one(self):
        self.backed_up()
        older = sorted(vault.root_info(self.home)["path"].glob("snapshot-*.obsnap"))[0]
        newer = older.with_name(older.name.replace(older.name.split("-")[1], "29991231T000000Z"))
        newer.write_bytes(b"OBSENC1\n" + b"damaged" * 10)
        shutil.rmtree(self.home)
        out = M.restore_latest(self.home, store=self.store)
        self.assertEqual(out["status"], "restored")
        self.assertEqual(Path(out["from"]).name, older.name)
        self.assertEqual(out["skipped"][0]["snapshot"], newer.name)

    def test_nothing_to_restore_is_said_plainly(self):
        elsewhere = self.base / "never backed up"
        self.assertEqual(M.restore_latest(elsewhere, store=self.store)["status"], "no-backup")

    def test_a_wrong_passphrase_moves_on_and_never_restores_garbage(self):
        self.backed_up()
        label = vault._workspace_label(self.home)
        shutil.rmtree(self.home)
        wrong = FakeStore()
        wrong.items[label] = "not the passphrase at all, but long enough"
        out = M.restore_latest(self.home, store=wrong)
        self.assertEqual(out["status"], "backups-found-locked")
        self.assertFalse(self.home.exists() and any(self.home.iterdir()))


# --- R4: the app follows the engine --------------------------------------------------------

def make_bundle(folder: Path, version: str, ident: str = A.BUNDLE_ID) -> Path:
    app = folder / A.APP_NAME
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": ident, "CFBundleShortVersionString": version}))
    (app / "Contents" / "MacOS" / "ProjectObservatory").write_text(f"synthetic {version}\n")
    return app


class FakeAppSystem:
    def __init__(self, teams=None, refuse="", running=False):
        self.teams, self.refuse, self.is_running, self.calls = teams or {}, refuse, running, []

    def team(self, app):
        return self.teams.get(app.name if app.name != A.APP_NAME else str(app.parent.name), "TEAM1")

    def verify(self, app):
        self.calls.append(("verify", app.parent.name))
        return self.refuse

    def running(self):
        return self.is_running

    def extract(self, zipped, into):
        version = zipped.name.split("-")[1]
        make_bundle(into, getattr(self, "zip_version", version), getattr(self, "zip_id", A.BUNDLE_ID))

    def copy(self, source, dest):
        shutil.copytree(source, dest, symlinks=True)

    def register(self, app):
        self.calls.append(("register", app.name))


class FakeFetcher:
    def __init__(self, version, zipped=b"synthetic zip bytes", digest_ok=True):
        self.version, self.zipped = version, zipped
        name = A.ZIP.format(version=version)
        self.sums = f"{hashlib.sha256(zipped).hexdigest()}  {name}\n".encode()
        self.digest_ok = digest_ok

    def get_json(self, url):
        name = A.ZIP.format(version=self.version)
        zdigest = hashlib.sha256(self.zipped if self.digest_ok else b"other").hexdigest()
        return {"tag_name": f"v{self.version}", "assets": [
            {"name": name, "browser_download_url": "https://example.invalid/app.zip",
             "digest": f"sha256:{zdigest}", "size": len(self.zipped)},
            {"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/SHA256SUMS",
             "digest": f"sha256:{hashlib.sha256(self.sums).hexdigest()}", "size": len(self.sums)}]}

    def fetch_file(self, url, dest, limit):
        data = self.sums if url.endswith("SHA256SUMS") else self.zipped
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)


class App(Base):
    def updater(self, installed="0.1.0", target="9.9.9", system=None, fetcher=None):
        apps = self.base / "Applications"
        apps.mkdir(exist_ok=True)
        app = make_bundle(apps, installed)
        return A.AppUpdater(self.home, system=system or FakeAppSystem(), fetcher=fetcher or FakeFetcher(target),
                            app=app, version=target), app

    def step(self, updater):
        state = {}
        out = updater.step(state, AT)
        self.assertEqual(state["app"]["result"], out["result"])
        return out

    def test_an_older_app_is_replaced_and_the_previous_kept(self):
        updater, app = self.updater()
        out = self.step(updater)
        self.assertEqual((out["result"], out["from"], out["version"]), ("updated", "0.1.0", "9.9.9"))
        self.assertEqual(A.bundle_info(app)["version"], "9.9.9")
        self.assertEqual(A.bundle_info(self.home / "store/app-previous" / A.APP_NAME)["version"], "0.1.0")
        self.assertFalse((self.home / "store/app-update").exists())
        self.assertIn(("register", A.APP_NAME), updater.system.calls)
        self.assertEqual(self.step(updater)["result"], "current")

    def test_a_running_app_waits_and_is_updated_after_it_quits(self):
        system = FakeAppSystem(running=True)
        updater, app = self.updater(system=system)
        self.assertEqual(self.step(updater)["result"], "waiting-for-quit")
        self.assertEqual(A.bundle_info(app)["version"], "0.1.0", "never swapped under a running app")
        self.assertTrue((self.home / "store/app-update/9.9.9" / A.APP_NAME).is_dir())
        system.is_running = False
        updater.fetcher = None  # the staged bundle is used, no second download
        self.assertEqual(self.step(updater)["result"], "updated")

    def test_a_bundle_from_another_team_never_replaces_the_app(self):
        system = FakeAppSystem()
        system.team = lambda app: "TEAM1" if "Applications" in str(app) and ".installing" not in str(app) \
            and "app-update" not in str(app) else "SOMEONE-ELSE"
        updater, app = self.updater(system=system)
        out = self.step(updater)
        self.assertEqual(out["result"], "refused")
        self.assertIn("not signed by the team", out["detail"])
        self.assertEqual(A.bundle_info(app)["version"], "0.1.0")

    def test_gatekeeper_refusal_digest_mismatch_and_wrong_bundles_are_refused(self):
        for system, fetcher, why in (
                (FakeAppSystem(refuse="Gatekeeper refused the bundle: rejected"), None, "Gatekeeper"),
                (FakeAppSystem(), FakeFetcher("9.9.9", digest_ok=False), "digest"),
        ):
            with self.subTest(why=why):
                shutil.rmtree(self.base / "Applications", ignore_errors=True)
                updater, app = self.updater(system=system, fetcher=fetcher)
                out = self.step(updater)
                self.assertEqual(out["result"], "refused")
                self.assertIn(why, out["detail"])
                self.assertEqual(A.bundle_info(app)["version"], "0.1.0")
        for attr, value, why in (("zip_version", "9.9.8", "version"), ("zip_id", "com.example.other", "names")):
            with self.subTest(why=why):
                shutil.rmtree(self.base / "Applications", ignore_errors=True)
                system = FakeAppSystem()
                setattr(system, attr, value)
                updater, _ = self.updater(system=system)
                out = self.step(updater)
                self.assertEqual(out["result"], "refused")
                self.assertIn(why, out["detail"])

    def test_an_unsigned_install_is_named_and_nothing_is_downloaded(self):
        system = FakeAppSystem()
        system.team = lambda app: None
        fetcher = FakeFetcher("9.9.9")
        fetcher.get_json = lambda url: self.fail("no download for an app that could never be matched")
        updater, _ = self.updater(system=system, fetcher=fetcher)
        self.assertEqual(self.step(updater)["result"], "unsigned-install")

    def test_a_refused_download_is_retried_once_a_day_not_every_hour(self):
        updater, _ = self.updater(system=FakeAppSystem(refuse="Gatekeeper refused the bundle: rejected"))
        state = {}
        self.assertEqual(updater.step(state, AT)["result"], "refused")
        self.assertEqual(updater.step(state, AT + datetime.timedelta(hours=1))["result"], "not-due")
        self.assertEqual(updater.step(state, AT + datetime.timedelta(hours=25))["result"], "refused")

    def test_an_app_opened_during_the_copy_is_not_replaced(self):
        system = FakeAppSystem()
        answers = iter([False, True])
        system.running = lambda: next(answers, True)
        updater, app = self.updater(system=system)
        self.assertEqual(self.step(updater)["result"], "waiting-for-quit")
        self.assertEqual(A.bundle_info(app)["version"], "0.1.0")
        self.assertFalse(app.parent.joinpath(f".{A.APP_NAME}.installing").exists())

    def test_an_app_that_is_absent_current_or_unwritable_is_left_alone(self):
        self.assertEqual(A.AppUpdater(self.home, app=None).step({}, AT)["result"], "not-installed")
        updater, _ = self.updater(installed="9.9.9")
        self.assertEqual(self.step(updater)["result"], "current")
        shutil.rmtree(self.base / "Applications")
        updater, app = self.updater(installed="0.1.0")
        app.parent.chmod(0o555)
        try:
            out = self.step(updater)
        finally:
            app.parent.chmod(0o755)
        self.assertEqual(out["result"], "not-writable")
        self.assertIn("by hand", out["detail"])


# --- R9: the bridge for installs before 0.17.0 -------------------------------------------------

HOOK = ROOT / "skill" / "plugins" / "observatory-log" / "hooks" / "session-start.sh"


class Bridge(Base):
    def fake_install(self, with_maintain: bool, check_exit: int = 10):
        venv = self.base / "venv"
        root = venv / "lib" / "python3" / "site-packages" / "observatory" / "engine"
        (root / "tools").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (venv / "bin").mkdir()
        os.symlink(sys.executable, venv / "bin" / "python")
        log = self.base / "calls.log"
        po = venv / "bin" / "project-observatory"
        po.write_text(f"#!/bin/bash\necho \"po $*\" >> '{log}'\n"
                      f"[ \"$3\" = \"--check\" ] && exit {check_exit}\nexit 0\n")
        po.chmod(0o755)
        if with_maintain:
            (root / "tools" / "maintain.py").write_text(
                f"import sys, os\nopen({str(log)!r}, 'a').write('maintain ' + ' '.join(sys.argv[1:]) + "
                f"' setup=' + os.environ.get('OBSERVATORY_SYSTEM_SETUP', '') + '\\n')\n")
        return root, log

    def run_hook(self, root, settings_auto=None):
        if settings_auto is not None:
            doc = config.load(self.home)
            doc["updates"] = {"auto": settings_auto}
            workspace.write_json(self.home / "config/settings.json", doc)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.base / "user"), "OBSERVATORY_ROOT": str(root),
               "OBSERVATORY_HOME": str(self.home), "XDG_STATE_HOME": str(self.base / "state")}
        subprocess.run(["/bin/bash", str(HOOK)], input="", env=env, capture_output=True, timeout=30, check=True)

    def wait_for(self, log, text, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if log.exists() and text in log.read_text():
                return log.read_text()
            time.sleep(0.1)
        return log.read_text() if log.exists() else ""

    def test_an_old_engine_is_updated_by_the_plugin_once_a_day(self):
        root, log = self.fake_install(with_maintain=False)
        self.run_hook(root)
        text = self.wait_for(log, "po full update --apply")
        self.assertIn("po full update --check", text)
        self.assertIn("po full update --apply", text)
        self.run_hook(root)  # within the day: nothing new
        time.sleep(1.0)
        self.assertEqual(log.read_text().count("--check"), 1)

    def test_an_old_engine_with_automatic_updates_off_is_left_alone(self):
        root, log = self.fake_install(with_maintain=False)
        self.run_hook(root, settings_auto=False)
        bridge_log = self.wait_for(self.base / "state" / "project-observatory" / "update-bridge.log", "off")
        self.assertIn("automatic updates are off", bridge_log)
        self.assertFalse(log.exists() and "--apply" in log.read_text())

    def test_an_up_to_date_old_engine_is_only_checked(self):
        root, log = self.fake_install(with_maintain=False, check_exit=0)
        self.run_hook(root)
        self.wait_for(self.base / "state" / "project-observatory" / "update-bridge.log", "check exit 0")
        self.assertNotIn("--apply", log.read_text())

    def test_sessions_starting_together_start_one_bridge(self):
        # Review F6: the claim is atomic.
        root, log = self.fake_install(with_maintain=False)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.base / "user"), "OBSERVATORY_ROOT": str(root),
               "OBSERVATORY_HOME": str(self.home), "XDG_STATE_HOME": str(self.base / "state")}
        procs = [subprocess.Popen(["/bin/bash", str(HOOK)], stdin=subprocess.DEVNULL, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(8)]
        for proc in procs:
            proc.wait(timeout=30)
        self.wait_for(log, "--apply")
        time.sleep(1.5)
        self.assertEqual(log.read_text().count("--check"), 1)

    def test_a_new_engine_gets_its_maintenance_hook_with_the_setup_flag(self):
        root, log = self.fake_install(with_maintain=True)
        self.run_hook(root)
        text = self.wait_for(log, "maintain hook")
        self.assertIn("maintain hook setup=1", text)
        self.assertNotIn("po full update", text)


if __name__ == "__main__":
    unittest.main()
