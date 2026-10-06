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
import release_signature
import update_events
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pgp_fixture

SIGNER = pgp_fixture.Key()

AT = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.timezone.utc)
SECRET_MARK = "zebra-orchard-" + "5521"


class FakeStore:
    """The OS credential store: a dict, and a log of what it was asked."""

    def __init__(self, fail=False):
        self.items, self.fail, self.calls = {}, fail, []

    def kind(self):
        return "fake"

    def put(self, label, value, *, replace=False):
        self.calls.append(("put", label))
        if self.fail:
            raise vault.BackupError("the fake store refused")
        # As the real stores do: without `replace` an existing item is never overwritten.
        if label in self.items and not replace:
            raise vault.BackupError("the fake store already holds " + label)
        self.items[label] = value
        return "fake"

    def get(self, label):
        self.calls.append(("get", label))
        return self.items.get(label)


class FakeCommands:
    """`full update --check` and `--apply`, answered from a script of exit codes."""

    def __init__(self, check=0, apply=0, latest="9.9.9"):
        self.check, self.apply_code, self.latest, self.calls = check, apply, latest, []

    def full(self, *args, timeout=0, env=None):
        self.calls.append((args, dict(env or {})))
        return self.check, {"target": self.latest}, "" if self.check in (0, 10) else "network down"

    def apply(self, *args, env=None):
        self.calls.append((("update", *args), dict(env or {})))
        return (self.apply_code, {"version": self.latest},
                "" if self.apply_code == 0 else "synthetic failure")


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
                                           "XDG_CONFIG_HOME": str(self.base / "xdg-config"),
                                           "OBSERVATORY_PRODUCT_LOG_DIR": str(self.base / "product-logs")},
                              clear=True)
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

def events():
    rows = update_events.read()
    for row in rows:
        assert set(row) <= {"at", "event", "code", "subject", "instance"}, row
    return [(r["event"], r["code"]) for r in rows]


class Switch(Base):
    """LC-16: the file `auto-update` in the home; absent = on, only `off` is off."""

    def test_automatic_updates_are_on_until_a_person_turns_them_off(self):
        self.assertTrue(M.auto_enabled(self.home))
        self.assertTrue(M.schedule_wanted(self.home))
        self.assertFalse((self.home / "auto-update").exists(), "a new workspace writes no switch")
        code, doc, _ = self.cli("auto-update", "off")
        self.assertEqual((code, doc["auto_update"]), (0, False))
        self.assertEqual((self.home / "auto-update").read_text(), "off\n")
        self.assertEqual(stat.S_IMODE((self.home / "auto-update").stat().st_mode), 0o600)
        self.assertNotIn("updates", config.load(self.home), "the switch is the file, not settings.json")
        self.assertEqual(doc["switch"]["label"], "Install updates automatically")
        code, doc, _ = self.cli("auto-update", "on")
        self.assertEqual((code, doc["auto_update"]), (0, True))
        code, doc, _ = self.cli("auto-update", "status")
        self.assertEqual(code, 0)
        self.assertIs(doc["auto_update"], True)
        self.assertEqual(doc["switch"]["source"], "file")
        self.assertIn("warnings", doc)
        self.assertEqual(events(), [("auto_update", "off"), ("auto_update", "on")])

    def test_only_the_word_off_turns_it_off(self):
        switch = self.home / "auto-update"
        for text, on in (("off", False), ("  OFF \n", False), ("Off", False), ("on", True), ("", True),
                         ("no", True), ("off please", True), ("false", True)):
            with self.subTest(text=text):
                switch.write_text(text)
                self.assertIs(M.auto_enabled(self.home), on)
        switch.unlink()
        target = self.base / "elsewhere"
        target.write_text("off")
        switch.symlink_to(target)
        self.assertTrue(M.auto_enabled(self.home), "a link in the switch's place is not a switch")

    def test_an_older_installs_setting_is_still_off_and_moves_into_the_file(self):
        M.set_setting(self.home, "auto", False)        # how 0.17 and 0.18 turned updates off
        self.assertFalse(M.auto_enabled(self.home))
        self.assertEqual(M.switch_status(self.home)["source"], "settings")
        code, doc, err = self.cli("auto-update", "status")
        self.assertEqual(code, 0)
        self.assertIs(doc["auto_update"], False)
        self.assertIn("moved", doc["migrated"])
        self.assertIn("moved", err, "the move is said, not done in silence")
        self.assertEqual((self.home / "auto-update").read_text(), "off\n")
        self.assertNotIn("auto", config.load(self.home).get("updates", {}))
        self.assertFalse(M.auto_enabled(self.home))
        # A file a person wrote wins over a setting left behind.
        M.set_setting(self.home, "auto", False)
        (self.home / "auto-update").write_text("on\n")
        self.assertTrue(M.auto_enabled(self.home))

    def test_an_update_a_reinstall_or_an_uninstall_never_writes_the_switch(self):
        commands = FakeCommands(check=10, apply=0)
        with patch.object(M, "_services", return_value=None):
            M.run_pass(self.home, at=AT, commands=commands, services=None, store=self.store, app=NoApp(),
                       sleep=lambda s: None)
        M.ensure(self.home, schedule=FakeSchedule(), store=self.store)
        M.unschedule(self.home, schedule=FakeSchedule())
        self.assertFalse((self.home / "auto-update").exists(), "never written back to on")
        (self.home / "auto-update").write_text("off\n")
        M.ensure(self.home, schedule=FakeSchedule(), store=self.store, explicit=True)
        M.run_pass(self.home, at=AT + datetime.timedelta(days=1), commands=commands, services=None,
                   store=self.store, app=NoApp(), sleep=lambda s: None)
        self.assertEqual((self.home / "auto-update").read_text(), "off\n", "and never to off either")
        # A snapshot and its restore carry the switch as it was (a rollback swaps the home).
        restored = self.base / "restored home"
        snap = workspace_upgrade.snapshot(self.home, self.base / "snap", writers_stopped=True)
        workspace_upgrade.restore(Path(snap["snapshot"]), restored)
        self.assertEqual((restored / "auto-update").read_text(), "off\n")

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


class UpdateLog(Base):
    """LC-16 "Log events": codes only, in the product's log folder, never the real one in tests."""

    def test_the_folder_is_the_products_and_overridable(self):
        self.assertEqual(update_events.directory(), self.base / "product-logs")
        with patch.dict(os.environ, {update_events.DIR_ENV: ""}), patch.object(sys, "platform", "darwin"):
            self.assertEqual(update_events.directory(),
                             self.base / "user" / "Library" / "Logs" / "Project Observatory")
        with patch.dict(os.environ, {update_events.DIR_ENV: "", "XDG_STATE_HOME": str(self.base / "state")}), \
                patch.object(sys, "platform", "linux"):
            self.assertEqual(update_events.directory(), self.base / "state" / "project-observatory" / "logs")

    def test_one_line_per_event_codes_only_owner_only(self):
        update_events.emit("update_check", "ready", base=self.home)
        update_events.emit("update_install", "started", subject="app", base=self.home)
        file = self.base / "product-logs" / update_events.FILE
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        rows = [json.loads(line) for line in file.read_text().splitlines()]
        self.assertEqual([(r["event"], r["code"], r["subject"]) for r in rows],
                         [("update_check", "ready", "engine"), ("update_install", "started", "app")])
        for row in rows:
            self.assertRegex(row["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertRegex(row["instance"], r"^[0-9a-f]{16}$")
            self.assertNotIn(str(self.home), json.dumps(row), "no path")
        with self.assertRaises(ValueError):
            update_events.emit("update_check", "something-else")

    def test_every_lc16_code_is_known(self):
        self.assertEqual({k: set(v) for k, v in update_events.EVENTS.items()}, {
            "update_check": {"current", "ready", "check_failed", "download_failed", "signature_failed",
                             "install_failed", "needs_migration"},
            "update_download": {"started", "done"},
            "update_install": {"started", "installed", "failed", "timeout"},
            "update_restart": {"requested", "refused"},
            "auto_update": {"on", "off"}})

    def test_a_folder_that_cannot_be_written_loses_the_line_not_the_caller(self):
        blocker = self.base / "a file"
        blocker.write_text("")
        with patch.dict(os.environ, {update_events.DIR_ENV: str(blocker / "logs")}):
            update_events.emit("auto_update", "on")      # no exception


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

    def test_a_lost_passphrase_file_comes_back_from_the_store_and_is_never_replaced(self):
        # Audit A01: generating a new one would overwrite the only copy every backup opens with.
        first = vault.ensure_passphrase(self.home, self.store)
        value = vault.passphrase(self.home)
        vault.passphrase_file(self.home).unlink()
        again = vault.ensure_passphrase(self.home, self.store)
        self.assertEqual(again["passphrase"], "recovered")
        self.assertEqual(vault.passphrase(self.home), value)
        self.assertEqual(self.store.items[first["label"]], value)

    def test_a_changed_passphrase_keeps_the_old_one_as_previous(self):
        out = vault.ensure_passphrase(self.home, self.store)
        old = vault.passphrase(self.home)
        vault.set_passphrase(self.home, "a new phrase a person chose, long enough")
        vault.ensure_passphrase(self.home, self.store)
        self.assertEqual(self.store.items[out["label"]], "a new phrase a person chose, long enough")
        self.assertEqual(self.store.items[out["label"] + vault.PREVIOUS], old)
        self.assertEqual(vault.stored_passphrases(self.store, out["label"]),
                         ["a new phrase a person chose, long enough", old])

    def test_a_store_that_cannot_be_read_is_never_written_over(self):
        class Unreadable(FakeStore):
            def get(self, label):
                raise vault.BackupError("the keychain is locked")
        store = Unreadable()
        vault.set_passphrase(self.home, "a phrase already in the workspace")
        out = vault.ensure_passphrase(self.home, store)
        self.assertIn("only in", out["warning"])
        self.assertFalse([c for c in store.calls if c[0] == "put"])

    def test_a_lost_file_and_a_silent_store_generate_nothing(self):
        # Audit A01 review: generating here made a passphrase that would later be
        # stored over the one every existing backup opens with.
        class Unreadable(FakeStore):
            def get(self, label):
                raise vault.BackupError("the keychain timed out")
        store = Unreadable()
        out = vault.ensure_passphrase(self.home, store)
        self.assertEqual((out["passphrase"], out["kept_outside"]), ("missing", None))
        self.assertIn("none was generated", out["warning"])
        self.assertIsNone(vault.passphrase(self.home))
        self.assertFalse([c for c in store.calls if c[0] == "put"])

    def test_every_earlier_passphrase_is_kept(self):
        # Audit A01 review: one `-previous` slot lost the first of two changes.
        out = vault.ensure_passphrase(self.home, self.store)
        first = vault.passphrase(self.home)
        for phrase in ("the second phrase, long enough", "the third phrase, long enough"):
            vault.set_passphrase(self.home, phrase)
            vault.ensure_passphrase(self.home, self.store)
        vault.ensure_passphrase(self.home, self.store)  # idempotent: nothing appended twice
        self.assertEqual(vault.stored_passphrases(self.store, out["label"]),
                         ["the third phrase, long enough", first, "the second phrase, long enough"])

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
            code, out, *err = answers.pop(0) if answers else (0, "")
            return subprocess.CompletedProcess(argv, code, out, err[0] if err else "")
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
        # A first copy never replaces an item already there; only a replace says -U.
        self.assertNotIn("-U", calls[0][1])
        run, calls = self.runner([(0, "")])
        store = vault.SecretStore(runner=run, platform="darwin", which=lambda name: "/usr/bin/" + name)
        store.put("private workspace-1234abcd", value, replace=True)
        self.assertIn("-U", calls[0][1])

    def test_a_keychain_that_does_not_answer_is_not_an_absent_item(self):
        # Audit A01 review: a timeout or a locked Keychain read as "no item", and a new
        # passphrase was then written over the only copy.
        def timing_out(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 30)
        store = vault.SecretStore(runner=timing_out, platform="darwin", which=lambda name: "/usr/bin/" + name)
        with self.assertRaises(vault.BackupError):
            store.get("ws-1")
        for answer in ((51, "", "User interaction is not allowed."), (36, ""), (0, "")):
            run, _ = self.runner([answer])
            store = vault.SecretStore(runner=run, platform="darwin", which=lambda name: "/usr/bin/" + name)
            with self.subTest(answer=answer), self.assertRaises(vault.BackupError):
                store.get("ws-1")
        run, _ = self.runner([(44, "", "The specified item could not be found in the keychain.")])
        store = vault.SecretStore(runner=run, platform="darwin", which=lambda name: "/usr/bin/" + name)
        self.assertIsNone(store.get("ws-1"), "exit 44 is the one answer that means absent")

    def test_secret_service_values_travel_on_stdin(self):
        # lookup (absent) → store → lookup
        run, calls = self.runner([(1, ""), (0, ""), (0, "the value\n")])
        store = vault.SecretStore(runner=run, platform="linux",
                                  which=lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None)
        self.assertEqual(store.put("ws-1", "the value"), "secret-service")
        self.assertEqual(store.get("ws-1"), "the value")
        self.assertEqual(calls[1][1], "the value")
        self.assertNotIn("the value", " ".join(calls[1][0]))
        run, _ = self.runner([(0, "an older value\n")])
        store = vault.SecretStore(runner=run, platform="linux",
                                  which=lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None)
        with self.assertRaises(vault.BackupError, msg="a first copy never replaces one"):
            store.put("ws-1", "the value")

    def test_a_secret_service_that_does_not_answer_falls_back_to_the_file(self):
        # Review F7: `secret-tool` installed, no keyring running.
        no_bus = (1, "", "Cannot autolaunch D-Bus without X11 $DISPLAY")
        run, calls = self.runner([no_bus, no_bus, no_bus, no_bus])
        store = vault.SecretStore(runner=run, platform="linux",
                                  which=lambda name: "/usr/bin/secret-tool" if name == "secret-tool" else None)
        self.assertEqual(store.put("ws-1", "kept anyway"), "file")
        self.assertEqual(store.get("ws-1"), "kept anyway")
        # No keyring AND no file: that is a store that did not answer, not an absent item.
        with self.assertRaises(vault.BackupError):
            store.get("ws-2")

    def test_the_file_store_is_owner_only_outside_the_workspace(self):
        store = vault.SecretStore(platform="linux", which=lambda name: None)
        self.assertEqual(store.put("ws-1", "file value"), "file")
        file = self.base / "xdg-config" / "project-observatory" / "backup-passphrases" / "ws-1"
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(file.parent.stat().st_mode), 0o700)
        self.assertNotIn(self.home, file.parents)
        self.assertEqual(store.get("ws-1"), "file value")
        with self.assertRaises(vault.BackupError, msg="a first copy never replaces the file"):
            store.put("ws-1", "another value")
        file.chmod(0o644)
        with self.assertRaises(vault.BackupError, msg="a copy others can read is not trusted, nor absent"):
            store.get("ws-1")

    def test_a_label_that_could_break_the_command_is_refused(self):
        store = vault.SecretStore(platform="linux", which=lambda name: None)
        for bad in ('ws"; rm', "ws\nx", ""):
            with self.assertRaises(vault.BackupError):
                store.put(bad, "v")


class SuiteSandbox(unittest.TestCase):
    """Audit A08 review: a shell started by the agent plugin carries OBSERVATORY_HOME,
    OBSERVATORY_ROOT and OBSERVATORY_BACKUPS pointing at the live workspace, and a suite
    run from it kept them. tests/tmp.py keeps only paths inside a temporary directory."""

    def run_tmp(self, env):
        code = ("import os, json, tmp; print(json.dumps({k: os.environ.get(k) for k in "
                "('OBSERVATORY_HOME', 'OBSERVATORY_ROOT', 'OBSERVATORY_BACKUPS', 'OBSERVATORY_SYSTEM_SETUP')}))")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("OBSERVATORY_")}
        p = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent,
                           env={**clean, **env}, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout), p.stderr

    def test_a_real_workspace_in_the_environment_is_replaced(self):
        # A path no temporary directory holds, whatever $HOME the suite runs under.
        real = "/srv/observatory-not-a-sandbox/project-observatory-full"
        got, err = self.run_tmp({"OBSERVATORY_HOME": real, "OBSERVATORY_ROOT": real,
                                 "OBSERVATORY_BACKUPS": "/srv/observatory-not-a-sandbox/backups"})
        for name in ("OBSERVATORY_HOME", "OBSERVATORY_BACKUPS"):
            self.assertNotEqual(got[name], real, name)
            self.assertTrue(got[name].startswith((tempfile.gettempdir(), "/tmp", "/private/", "/var/")), got)
        self.assertIsNone(got["OBSERVATORY_ROOT"])
        self.assertEqual(got["OBSERVATORY_SYSTEM_SETUP"], "0")
        self.assertIn("never runs against a real workspace", err)

    def test_a_temporary_home_handed_over_is_kept(self):
        home = os.path.join(tempfile.mkdtemp(prefix="observatory-kept-"), "ws")
        self.addCleanup(shutil.rmtree, os.path.dirname(home), True)
        got, err = self.run_tmp({"OBSERVATORY_HOME": home})
        self.assertEqual(os.path.realpath(got["OBSERVATORY_HOME"]), os.path.realpath(home))
        self.assertEqual(err, "")


class ApplyProcess(Base):
    """`Commands.apply` with a stand-in interpreter: what the real child is given."""

    def fake_python(self, body: str) -> Path:
        script = self.base / "fake-python"
        script.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
        script.chmod(0o700)
        return script

    def test_the_apply_runs_in_its_own_session_and_its_answer_is_read_back(self):
        # Audit A03: a signal to the pass's process group must not reach the update.
        python = self.fake_python(
            "print(json.dumps({'version': '9.9.9', 'sid': os.getsid(0), 'argv': sys.argv[1:],\n"
            "  'in_pass': os.environ.get('OBSERVATORY_MAINTENANCE_PASS'),\n"
            "  'setup': os.environ.get('OBSERVATORY_SYSTEM_SETUP'), 'stdin': sys.stdin.read()}))\n"
            "print('last line on stderr', file=sys.stderr)\nsys.exit(5)")
        code, doc, err = M.Commands(self.home, python=str(python)).apply(
            "--apply", "--writers-stopped", env={"OBSERVATORY_SYSTEM_SETUP": "1"})
        self.assertEqual((code, doc["version"], err), (5, "9.9.9", "last line on stderr"))
        self.assertNotEqual(doc["sid"], os.getsid(0))
        self.assertEqual(doc["argv"][-4:], ["full", "update", "--apply", "--writers-stopped"])
        self.assertEqual((doc["in_pass"], doc["setup"], doc["stdin"]), ("1", "1", ""))
        # Beside the home, not inside it: a rollback's swap must not carry them away (A03 review).
        for path in M.apply_output_paths(self.home):
            self.assertEqual(path.parent, self.home.parent)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse((self.home / "store/logs/update-apply.out").exists())

    def test_an_interpreter_that_cannot_start_is_an_answer_not_a_crash(self):
        code, doc, err = M.Commands(self.home, python=str(self.base / "missing-python")).apply("--apply")
        self.assertEqual((code, doc), (125, {}))
        self.assertIn("Error", err)

    def test_output_that_is_not_json_reads_as_empty(self):
        python = self.fake_python("print('not json')")
        self.assertEqual(M.Commands(self.home, python=str(python)).apply("--apply")[:2], (0, {}))


# --- R2/R7: one pass -----------------------------------------------------------------

class Pass(Base):
    def setUp(self):
        super().setUp()
        launchd = patch.object(M, "_services", return_value=object())
        launchd.start()
        self.addCleanup(launchd.stop)

    def run_pass(self, commands, at=AT, services=None, app=None):
        return M.run_pass(self.home, at=at, commands=commands, services=services, store=self.store,
                          app=app or NoApp(), sleep=lambda s: None)

    def test_codes_match_the_updater(self):
        import engine_update as eu
        self.assertEqual((M.UPDATE_OK, M.UPDATE_FAILED, M.UPDATE_REFUSED, M.UPDATE_UNDETERMINED,
                          M.UPDATE_NEEDS_PERSON, M.UPDATE_SERVICES, M.UPDATE_HELD, M.UPDATE_AVAILABLE),
                         (eu.EXIT_OK, eu.EXIT_FAILED, eu.EXIT_REFUSED, eu.EXIT_UNDETERMINED,
                          eu.EXIT_NEEDS_PERSON, eu.EXIT_SERVICES, eu.EXIT_HELD, eu.EXIT_UPDATE_AVAILABLE))

    def test_up_to_date_checks_on_its_cadence_and_takes_the_daily_backup(self):
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
        self.assertEqual([c[0] for c in commands.calls], [("update", "--check"), ("update", "--apply", "--unattended")])
        self.assertEqual(commands.calls[1][1].get("OBSERVATORY_SYSTEM_SETUP"), "1")
        state = M.read_state(self.home)
        self.assertEqual((state["update"]["from"], state["update"]["to"]), (config.VERSION, "9.9.9"))
        self.assertEqual(state["snapshot"]["result"], "before-upgrade", "the update's own snapshot counts")
        self.assertNotIn("app", report, "the new release's code handles the app on the next pass")

    def test_without_launchd_the_update_is_told_there_is_nothing_to_stop(self):
        # Audit A02: on Linux `--apply` alone refuses ("launchd is absent") every day.
        commands = FakeCommands(check=10, apply=0)
        with patch.object(M, "_services", return_value=None):
            self.assertEqual(self.run_pass(commands)["update"]["result"], "updated")
        self.assertEqual(commands.calls[1][0], ("update", "--apply", "--unattended", "--writers-stopped"))

    def test_an_update_already_running_is_left_alone_and_tried_the_next_hour(self):
        commands = FakeCommands(check=10)
        with patch.object(M, "update_running", return_value=True):
            self.assertEqual(self.run_pass(commands)["update"]["result"], "another-update-running")
        self.assertEqual([c[0] for c in commands.calls], [("update", "--check")])
        self.assertEqual(M.read_state(self.home)["update"]["result"], "another-update-running")
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=1))
        self.assertEqual(later["update"]["result"], "updated")

    def test_failures_are_forgotten_once_the_engine_moved_on(self):
        commands = FakeCommands(check=10, apply=1)
        self.run_pass(commands)
        self.assertEqual(M.read_state(self.home)["update"]["failures"], 1)
        state = M.read_state(self.home)
        state["update"]["from"] = "0.0.1"  # recorded by an older engine than this one
        M.write_state(self.home, state)
        self.run_pass(FakeCommands(check=0), at=AT + datetime.timedelta(days=1))
        self.assertNotIn("failures", M.read_state(self.home)["update"])
        self.assertFalse(any("did not complete" in w
                             for w in M.status(self.home, schedule=FakeSchedule())["warnings"]))

    def test_after_an_update_the_check_reads_as_done(self):
        self.run_pass(FakeCommands(check=10, apply=0))
        self.assertEqual(M.read_state(self.home)["check"]["result"], "updated")

    def test_an_apply_the_pass_did_not_see_end_is_read_back_from_the_journal(self):
        # Audit A03: the pass was stopped; the update ran on in its own session.
        state = M.read_state(self.home)
        state["update_in_flight"] = {"at": "2026-10-05T11:00:00Z", "to": "9.9.9"}
        M.write_state(self.home, state)
        journal = self.home / "store" / "logs" / "update.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({"at": "2026-10-04T11:00:00Z", "event": "rolled-back"}) + "\n"
                           + json.dumps({"at": "2026-10-05T11:20:00Z", "event": "updated",
                                         "from": "0.1.0", "to": "9.9.9"}) + "\n")
        self.run_pass(FakeCommands(check=0))
        update = M.read_state(self.home)["update"]
        self.assertEqual((update["result"], update["to"], update["reconciled"]), ("updated", "9.9.9", True))
        self.assertNotIn("update_in_flight", M.read_state(self.home))

    def test_a_rolled_back_apply_is_read_from_the_restored_journal(self):
        # A03 review: `rolled-back` went into the failed copy with the swap; the journal of
        # the restored home ends with `undo-ended`, written after it.
        state = M.read_state(self.home)
        state["update_in_flight"] = {"at": "2026-10-05T11:00:00Z", "to": "9.9.9"}
        M.write_state(self.home, state)
        journal = self.home / "store" / "logs" / "update.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        for outcome, result in (("rolled-back", "failed-rolled-back"), ("needs-person", "needs-person")):
            journal.write_text(json.dumps({"at": "2026-10-05T10:00:00Z", "event": "tick"}) + "\n"
                               + json.dumps({"at": "2026-10-05T11:30:00Z", "event": "workspace-restored"}) + "\n"
                               + json.dumps({"at": "2026-10-05T11:30:01Z", "event": "undo-ended",
                                             "outcome": outcome, "error": "pip: connection reset",
                                             "from": "0.1.0", "to": "9.9.9"}) + "\n")
            got = M.reconcile_in_flight(self.home, {"update_in_flight": {"at": "2026-10-05T11:00:00Z"}})
            with self.subTest(outcome=outcome):
                self.assertEqual((got["result"], got["detail"]), (result, "pip: connection reset"))

    def test_an_in_flight_apply_with_no_final_entry_is_a_failure_not_silence(self):
        state = M.read_state(self.home)
        state["update_in_flight"] = {"at": "2026-10-05T11:00:00Z", "to": "9.9.9"}
        M.write_state(self.home, state)
        with patch.object(M, "update_running", return_value=True):
            self.assertIsNone(M.reconcile_in_flight(self.home, state), "still running: nothing to read yet")
        self.assertEqual(M.reconcile_in_flight(self.home, state)["result"], "failed")

    def test_the_daily_snapshot_waits_while_an_update_runs(self):
        # Audit A12: both stop and start the same jobs. The real lock, held as an
        # update holds it.
        import engine_update
        with engine_update.update_lock(self.home):
            report = self.run_pass(FakeCommands(check=0), services=FakeServices())
        self.assertEqual(report["snapshot"]["result"], "deferred")

    def test_an_update_cannot_start_while_the_snapshot_has_the_jobs_down(self):
        # A12 review: the check alone let an update start mid-snapshot; the snapshot's
        # `finally` then started the jobs in the middle of the install.
        import engine_update
        seen = {}
        real = workspace_upgrade.snapshot

        def snapshot(*a, **kw):
            seen["held"] = engine_update.update_lock_held(self.home)
            with self.assertRaises(engine_update.UpdateError):
                with engine_update.update_lock(self.home):
                    pass
            return real(*a, **kw)
        with patch.object(workspace_upgrade, "snapshot", side_effect=snapshot):
            report = self.run_pass(FakeCommands(check=0), services=FakeServices())
        self.assertTrue(seen.get("held"), "the snapshot holds the update lock while the jobs are down")
        self.assertEqual(report["snapshot"]["result"], "taken", report["snapshot"])
        self.assertFalse(engine_update.update_lock_held(self.home), "and lets it go after")

    def test_a_failed_snapshot_is_said_and_cleared_by_the_next_good_one(self):
        with patch.object(workspace_upgrade, "snapshot", side_effect=RuntimeError("synthetic disk full")):
            self.run_pass(FakeCommands(check=0), services=FakeServices())
        warnings = M.status(self.home, schedule=FakeSchedule())["warnings"]
        self.assertTrue(any("daily backup failed" in w for w in warnings), warnings)
        self.run_pass(FakeCommands(check=0), services=FakeServices(), at=AT + datetime.timedelta(hours=1))
        self.assertNotIn("snapshot_failure", M.read_state(self.home))
        self.assertFalse(any("daily backup failed" in w
                             for w in M.status(self.home, schedule=FakeSchedule())["warnings"]))

    def test_services_that_start_again_clear_the_warning(self):
        self.run_pass(FakeCommands(check=0), services=FakeServices(start_ok=False))
        self.assertTrue(M.read_state(self.home).get("services_not_restarted"))
        self.run_pass(FakeCommands(check=0), services=FakeServices(), at=AT + datetime.timedelta(days=1))
        self.assertFalse(M.read_state(self.home).get("services_not_restarted"))

    def test_the_passphrase_outcome_is_kept_for_status(self):
        self.run_pass(FakeCommands(check=0))
        doc = M.status(self.home, schedule=FakeSchedule())
        self.assertEqual(doc["passphrase"]["kept_outside"], "fake")
        self.assertNotIn(vault.passphrase(self.home), json.dumps(doc))

    def test_the_pass_rotates_its_own_logs_and_the_hooks(self):
        # Audit A19: the tick rotates store/logs, and the tick is off by default.
        import log_policy
        big = self.home / "store" / "logs" / "maintenance.log"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"x" * 2048)
        hooks = self.base / "user" / ".local" / "state" / "project-observatory"
        hooks.mkdir(parents=True)
        (hooks / "update-bridge.log").write_bytes(b"y" * 2048)
        with patch.object(log_policy, "MAX_BYTES", 1024):
            self.run_pass(FakeCommands(check=0))
        self.assertTrue((big.parent / "maintenance.log.1").exists())
        self.assertTrue((hooks / "update-bridge.log.1").exists())

    def test_a_failed_update_is_retried_at_the_next_check_not_the_next_hour(self):
        commands = FakeCommands(check=10, apply=1)
        self.assertEqual(self.run_pass(commands)["update"]["result"], "failed-rolled-back")
        self.assertEqual(M.read_state(self.home)["update"]["failures"], 1)
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=1))["update"]["result"], "not-due")
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=24))["update"]["result"],
                         "failed-rolled-back")
        self.assertEqual(M.read_state(self.home)["update"]["failures"], 2)
        status = M.status(self.home, schedule=FakeSchedule())
        self.assertTrue(any("failed" in w for w in status["warnings"]))

    def test_an_update_refused_for_a_moment_is_retried_the_next_hour(self):
        # Seen live: the pre-update copy was torn by a session server and the update was
        # refused (exit 2) with its reason only in the JSON; it then waited a day.
        commands = FakeCommands(check=10, apply=2)
        commands.apply_orig = commands.apply

        def apply(*args, env=None):
            code, doc, err = commands.apply_orig(*args, env=env)
            return code, {"status": "refused", "error": "RuntimeError: Database snapshot integrity verification failed"}, ""
        commands.apply = apply
        self.assertEqual(self.run_pass(commands)["update"]["result"], "refused")
        self.assertIn("integrity verification failed", M.read_state(self.home)["update"]["detail"])
        commands.apply_code = 0
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=1))
        self.assertEqual(later["update"]["result"], "updated", "a transient refusal is tried again the next hour")

    def test_a_refusal_for_good_waits_a_day(self):
        commands = FakeCommands(check=10, apply=2)
        self.run_pass(commands)
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=1))["update"]["result"], "not-due")

    def test_an_update_that_needs_a_person_stops_the_attempts(self):
        commands = FakeCommands(check=10, apply=4)
        self.assertEqual(self.run_pass(commands)["update"]["result"], "needs-person")
        later = self.run_pass(commands, at=AT + datetime.timedelta(days=3))
        self.assertEqual(later["update"]["result"], "waiting-for-person")
        self.assertEqual(len(commands.calls), 2)
        self.assertTrue(any("needs a person" in w for w in M.status(self.home, schedule=FakeSchedule())["warnings"]))

    def test_no_network_is_undetermined_and_never_up_to_date(self):
        commands = FakeCommands(check=3)
        report = self.run_pass(commands)
        self.assertEqual(report["update"]["result"], "undetermined")
        self.assertEqual(M.read_state(self.home)["check"]["detail"], "network down")
        # Seen live: a check that timed out on a loaded machine waited a whole day. It is
        # tried again the next hour.
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(minutes=30))["update"]["result"], "not-due")
        commands.check = 10
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=1))["update"]["result"], "updated")

    def test_turned_off_means_no_check_and_the_backup_still_runs(self):
        M.set_setting(self.home, "auto", False)
        commands = FakeCommands(check=10)
        report = self.run_pass(commands, services=FakeServices())
        self.assertEqual(report["update"]["result"], "off")
        self.assertEqual(commands.calls, [])
        self.assertEqual(report["snapshot"]["result"], "taken")

    # --- LC-16 -------------------------------------------------------------------------

    def test_the_switch_set_to_off_stops_checks_downloads_and_installs_the_apps_too(self):
        (self.home / "auto-update").write_text("off\n")
        commands = FakeCommands(check=10)
        app = RecordingApp()
        report = self.run_pass(commands, services=FakeServices(), app=app)
        self.assertEqual((report["update"]["result"], report["app"]["result"]), ("off", "off"))
        self.assertEqual((commands.calls, app.calls), ([], []), "no check, no download, no install")
        self.assertEqual(report["snapshot"]["result"], "taken", "the daily backup keeps running")

    def test_a_check_every_six_hours(self):
        self.assertEqual(M.CHECK_EVERY, datetime.timedelta(hours=6))
        commands = FakeCommands(check=0)
        self.run_pass(commands)
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=5, minutes=59))["update"]["result"],
                         "not-due")
        self.assertEqual(self.run_pass(commands, at=AT + datetime.timedelta(hours=6))["update"]["result"], "up-to-date")
        self.assertEqual(len(commands.calls), 2)

    def test_a_failed_check_is_retried_once_within_the_hour_then_every_six_hours(self):
        commands = FakeCommands(check=3)
        self.assertEqual(self.run_pass(commands)["update"]["result"], "undetermined")
        hour = AT + datetime.timedelta(hours=1)
        self.assertEqual(self.run_pass(commands, at=hour)["update"]["result"], "undetermined")
        self.assertEqual(M.read_state(self.home)["check"]["failures"], 2)
        self.assertEqual(self.run_pass(commands, at=hour + datetime.timedelta(hours=1))["update"]["result"], "not-due",
                         "one retry, then back to the six-hour cadence")
        self.assertEqual(self.run_pass(commands, at=hour + datetime.timedelta(hours=6))["update"]["result"],
                         "undetermined")
        commands.check = 0
        later = hour + datetime.timedelta(hours=12)
        self.assertEqual(self.run_pass(commands, at=later)["update"]["result"], "up-to-date")
        self.assertNotIn("failures", M.read_state(self.home)["check"])

    def test_the_schedulers_first_check_waits_90_seconds_after_the_start(self):
        order = []
        commands = FakeCommands(check=0)
        original = commands.full

        def full(*args, **kw):
            order.append("check")
            return original(*args, **kw)
        commands.full = full
        report = M.run_pass(self.home, at=AT, commands=commands, services=None, store=self.store, app=NoApp(),
                            sleep=lambda s: order.append(("sleep", s)), first_check_delay=90,
                            clock=lambda: M.PROCESS_STARTED + 10)
        self.assertEqual(report["update"]["result"], "up-to-date")
        self.assertEqual(order[:2], [("sleep", 80), "check"], "the check waits, then runs")
        order.clear()
        M.run_pass(self.home, at=AT + datetime.timedelta(hours=1), commands=commands, services=None,
                   store=self.store, app=NoApp(), sleep=lambda s: order.append(("sleep", s)),
                   first_check_delay=90, clock=lambda: M.PROCESS_STARTED + 10)
        self.assertNotIn("check", order)
        self.assertFalse([o for o in order if isinstance(o, tuple) and o[1] == 80], "no wait when no check is due")
        code = M.parser().parse_args(["run", "--first-check-delay", "90"])
        self.assertEqual(code.first_check_delay, 90)

    def receipt(self, at, recent=True, silent=480):
        raw = self.home / "store" / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "serverd.json").write_text(json.dumps({"at": M.iso(at), "silent_after_s": silent,
                                                      "clients": {"recent": recent, "window_s": 300}}))

    def test_a_live_client_of_the_server_defers_the_activation_to_the_next_pass(self):
        self.receipt(AT, recent=True)
        commands = FakeCommands(check=10, apply=0)
        report = self.run_pass(commands)
        self.assertEqual(report["update"]["result"], "deferred")
        self.assertEqual([c[0] for c in commands.calls], [("update", "--check")], "nothing installed")
        self.assertIn(("update_restart", "refused"), events())
        state = M.read_state(self.home)
        self.assertEqual((state["update"]["result"], state["update"]["to"]), ("deferred", "9.9.9"))
        self.assertIn("deferred", [w["code"] for w in M.status(self.home, schedule=FakeSchedule())["warning_items"]])
        # The next hourly pass, the client gone: installed.
        self.receipt(AT + datetime.timedelta(hours=1), recent=False)
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=1))
        self.assertEqual(later["update"]["result"], "updated")
        self.assertEqual(commands.calls[-1][0], ("update", "--apply", "--unattended"))

    def test_a_running_tick_defers_the_update_instead_of_stopping_it(self):
        # 2026-10-06: an automatic update stopped the tick that had started a minute
        # earlier, and it never finished. The real lock, held as a tick holds it.
        commands = FakeCommands(check=10, apply=0)
        with workspace_upgrade.operation_lock(self.home):
            report = self.run_pass(commands)
        self.assertEqual(report["update"]["result"], "deferred")
        self.assertIn("tick", report["update"]["detail"])
        self.assertEqual([c[0] for c in commands.calls], [("update", "--check")], "nothing installed")
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=1))
        self.assertEqual(later["update"]["result"], "updated", "the next pass, the tick done")

    def test_a_running_tick_defers_the_update_and_is_never_stopped(self):
        # Seen live on 2026-10-06: an automatic update booted out a tick 57 s into its run.
        import fcntl
        lock = self.home / "store" / "tick.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # a tick is running
            commands = FakeCommands(check=10, apply=0)
            report = self.run_pass(commands)
            self.assertEqual(report["update"]["result"], "deferred")
            self.assertIn("tick", report["update"]["detail"])
            self.assertEqual([c[0] for c in commands.calls], [("update", "--check")], "no apply, nothing stopped")
        finally:
            os.close(fd)
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=1))
        self.assertEqual(later["update"]["result"], "updated", "the next pass, the tick done, installs")

    def test_a_silent_server_and_a_probe_are_no_clients(self):
        self.receipt(AT - datetime.timedelta(hours=1), recent=True)      # the server stopped an hour ago
        self.assertEqual(M.live_clients(self.home, AT), [])
        self.receipt(AT, recent=False)
        self.assertEqual(M.live_clients(self.home, AT), [])

    def test_a_memory_http_client_defers_and_a_refused_or_old_call_does_not(self):
        logs = self.home / "store" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        journal = logs / "access.jsonl"

        def row(at, allowed, binding="binding-1"):
            return json.dumps({"at": M.iso(at), "binding": binding, "tool": "memory.search", "allowed": allowed}) + "\n"
        journal.write_text(row(AT - datetime.timedelta(minutes=10), True) + row(AT - datetime.timedelta(seconds=30), False))
        self.assertEqual(M.live_clients(self.home, AT), [])
        journal.write_text(journal.read_text() + row(AT - datetime.timedelta(seconds=20), True))
        self.assertEqual(len(M.live_clients(self.home, AT)), 1)
        self.assertEqual(self.run_pass(FakeCommands(check=10))["update"]["result"], "deferred")

    def test_a_persons_update_applies_at_once_whatever_the_clients(self):
        # `full update --apply` from a person never asks maintenance; only the job defers.
        import engine_update as eu
        self.assertNotIn("live_clients", Path(eu.__file__).read_text())

    def test_a_held_release_is_shown_and_not_attempted_again_until_a_person_acts(self):
        step = "run the store migration by hand: https://example.com/runbook"

        class Held(FakeCommands):
            def apply(self, *args, env=None):
                self.calls.append((("update", *args), dict(env or {})))
                return 6, {"status": "held", "version": self.latest, "needs_person": step}, ""
        commands = Held(check=10, latest="9.9.9")
        report = self.run_pass(commands)
        self.assertEqual(report["update"]["result"], "held")
        state = M.read_state(self.home)
        self.assertEqual((state["update"]["needs_person"], state["update"]["to"]), (step, "9.9.9"))
        self.assertNotIn("failures", state["update"], "a held release is not a failure")
        items = {w["code"]: w for w in M.status(self.home, schedule=FakeSchedule())["warning_items"]}
        self.assertEqual((items["needs-migration"]["version"], items["needs-migration"]["step"]), ("9.9.9", step))
        later = self.run_pass(commands, at=AT + datetime.timedelta(hours=6))
        self.assertEqual(later["update"]["result"], "held")
        self.assertEqual([c[0][1] for c in commands.calls], ["--check", "--apply", "--check"],
                         "verified once; the same release is not downloaded again")
        # The person did the step and updated: the engine now runs the held release.
        state = M.read_state(self.home)
        state["update"]["to"] = config.VERSION
        M.write_state(self.home, state)
        self.assertNotIn("needs-migration",
                         [w["code"] for w in M.status(self.home, schedule=FakeSchedule())["warning_items"]])
        after = self.run_pass(FakeCommands(check=0), at=AT + datetime.timedelta(hours=12))
        self.assertEqual(after["update"]["result"], "up-to-date")
        self.assertEqual(M.read_state(self.home)["update"]["result"], "resolved-by-person")

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

    def test_a_torn_database_copy_is_retried_not_fatal(self):
        # Seen live on 2026-10-05: a session MCP server wrote to the store during the copy,
        # the copy failed its check ("database disk image is malformed") and the pass crashed.
        import sqlite3
        from store import compatibility
        real, calls = compatibility.verify_database, []

        def flaky(conn):
            calls.append(1)
            # Torn every time within one snapshot: only the pass's own retry can recover.
            if len(calls) <= workspace_upgrade.COPY_ATTEMPTS:
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real(conn)
        with patch.object(compatibility, "verify_database", flaky):
            report = self.run_pass(FakeCommands(), services=FakeServices())
        self.assertEqual(report["snapshot"]["result"], "taken")
        self.assertGreater(len(calls), workspace_upgrade.COPY_ATTEMPTS, "the snapshot was taken again")

    def test_copy_database_makes_a_torn_copy_again_and_stops_at_a_missing_package(self):
        import sqlite3
        from store import compatibility
        src = self.home / "store" / "observatory.db"
        real, calls = compatibility.verify_database, []

        def flaky(conn):
            calls.append(1)
            if len(calls) < workspace_upgrade.COPY_ATTEMPTS:
                raise RuntimeError("Database snapshot integrity verification failed")
            return real(conn)
        with patch.object(compatibility, "verify_database", flaky):
            workspace_upgrade.copy_database(src, self.base / "copy.db")
        self.assertEqual(len(calls), workspace_upgrade.COPY_ATTEMPTS)
        self.assertTrue((self.base / "copy.db").is_file())
        with patch.object(compatibility, "verify_database",
                          side_effect=RuntimeError("install the locked sqlite-vec dependency")):
            with self.assertRaisesRegex(RuntimeError, "sqlite-vec"):
                workspace_upgrade.copy_database(src, self.base / "copy2.db")
        self.assertFalse((self.base / "copy2.db").exists(), "a refused copy leaves nothing behind")

    def test_an_unexpected_error_in_the_backup_is_recorded_and_the_pass_completes(self):
        services = FakeServices()
        with patch.object(workspace_upgrade, "snapshot", side_effect=RuntimeError("synthetic surprise")):
            report = self.run_pass(FakeCommands(), services=services)
        self.assertEqual(report["snapshot"]["result"], "failed")
        self.assertIn("synthetic surprise", M.read_state(self.home)["snapshot_failure"]["detail"])
        self.assertIn("pass", M.read_state(self.home), "the pass recorded itself")
        self.assertEqual(sorted(c for c in services.calls if c[0] == "start"),
                         [("start", "server"), ("start", "tick")])

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


class RecordingApp:
    def __init__(self):
        self.calls = []

    def step(self, state, at):
        self.calls.append(at)
        return {"result": "current"}


# --- R3: the schedule -------------------------------------------------------------------

class Schedule(Base):
    def test_the_launchd_job_runs_hourly_and_is_never_a_managed_job(self):
        sched = M.LaunchdSchedule(self.home)
        doc = sched.document()
        self.assertTrue(doc["Label"].endswith(".maintain"))
        self.assertEqual(doc["ProgramArguments"][1:], [str(ROOT / "tools" / "maintain.py"), "run",
                                                       "--first-check-delay", "90"])
        self.assertEqual((doc["StartInterval"], doc["RunAtLoad"]), (3600, True))
        # No Background/LowPriorityIO throttling: it stretched a 2 s check past 600 s live.
        self.assertEqual(doc["ProcessType"], "Standard")
        self.assertNotIn("LowPriorityIO", doc)
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
        # A stop or a logout must not take the detached update down with the pass (A03 review).
        self.assertIn("KillMode=process", service.splitlines())
        self.assertIn("OnUnitActiveSec=60min", timer)
        # LC-16: the first pass, and its check, 90 seconds after the user manager starts.
        self.assertIn("OnStartupSec=90s", timer.splitlines())
        self.assertNotIn("OnBootSec=10min", timer)
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
        with patch.object(M.sys, "stdin") as stdin, patch.object(M, "_real_home", return_value=True):
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

    def test_a_childs_home_beside_its_own_temporary_directory_is_still_a_tests(self):
        # Audit A24: the test above patches `gettempdir` in-process, so it cannot see what a
        # runner really does — hand a child TMPDIR=<work>/tmp and a workspace at
        # <work>/runtime. The child's temporary directory is then a SIBLING of its home, not
        # an ancestor, and a terminal decided. Here a real child, at a real terminal (a pty),
        # with the account's own $HOME, is asked about exactly that layout under the
        # operating system's own temporary root.
        import pty
        import pwd
        if sys.platform == "darwin":
            root = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                                  text=True, timeout=30).stdout.strip()
        else:
            root = "/tmp"
        work = Path(tempfile.mkdtemp(prefix="observatory-guard-", dir=root)).resolve()
        self.addCleanup(shutil.rmtree, work, True)
        (work / "tmp").mkdir()
        probe = ("import sys, tempfile, maintenance\nfrom pathlib import Path\n"
                 "print(sys.stdin.isatty(), maintenance._real_home(), tempfile.gettempdir(),"
                 " maintenance.system_setup_allowed(Path(sys.argv[1])))")
        primary, secondary = pty.openpty()
        try:
            p = subprocess.run([sys.executable, "-c", probe, str(work / "runtime")], cwd=ROOT, stdin=secondary,
                               capture_output=True, text=True, timeout=60,
                               env={"PATH": "/usr/bin:/bin", "HOME": pwd.getpwuid(os.getuid()).pw_dir,
                                    "TMPDIR": str(work / "tmp"), "PYTHONDONTWRITEBYTECODE": "1"})
        finally:
            os.close(primary)
            os.close(secondary)
        tty, real, temp, allowed = (p.stdout.split() + ["?"] * 4)[:4]
        self.assertEqual((tty, real), ("True", "True"), "the child must see a terminal and its own home: " + p.stderr)
        self.assertEqual(Path(temp).resolve(), work / "tmp", "the child's temporary directory is the sibling")
        self.assertEqual(allowed, "False", "a workspace under the system's temporary root is a test's")

    def test_a_home_that_is_not_the_accounts_own_never_touches_the_machine(self):
        # Audit A07: a test or sandbox points $HOME elsewhere; launchd and the Keychain are
        # still the account's, so a terminal is not enough.
        self.assertFalse(M._real_home(), "Base points $HOME into its temporary directory")
        with patch.object(M.sys, "stdin") as stdin, \
                patch.object(M.tempfile, "gettempdir", return_value=str(self.base / "elsewhere")):
            stdin.isatty.return_value = True
            self.assertFalse(M.system_setup_allowed(Path("/opt/synthetic-observatory-home")))
        self.assertFalse(M.probes_allowed())
        with patch.object(M, "_real_home", return_value=True):
            self.assertTrue(M.probes_allowed())
            with patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "0"}):
                self.assertFalse(M.probes_allowed())

    def test_status_does_not_probe_launchd_when_it_may_not(self):
        with patch.object(M, "schedule_for", side_effect=AssertionError("probed")):
            doc = M.status(self.home)
        self.assertEqual(doc["schedule"]["probed"], False)
        self.assertFalse(any("not scheduled" in w for w in doc["warnings"]))

    def test_scheduled_means_installed_and_loaded(self):
        class Unloaded(FakeSchedule):
            def status(self):
                return {"kind": "fake", "installed": True, "loaded": False}
        doc = M.status(self.home, schedule=Unloaded())
        self.assertFalse(doc["scheduled"])
        self.assertTrue(any("not scheduled" in w for w in doc["warnings"]))
        schedule = FakeSchedule()
        schedule.install()
        doc = M.status(self.home, schedule=schedule)
        self.assertTrue(doc["scheduled"])
        self.assertEqual(doc["versions"]["engine"], config.VERSION)

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

    def test_maintain_run_and_hook_refuse_without_a_person_or_the_flag(self):
        # Audit A07 review: both wrote the Keychain and bootstrapped launchd from a test.
        for action in ("run", "hook"):
            with self.subTest(action=action), \
                    patch.object(M, "run_pass", side_effect=AssertionError("must not run")), \
                    patch.object(M, "ensure", side_effect=AssertionError("must not ensure")), \
                    patch.dict(os.environ, {"OBSERVATORY_SYSTEM_SETUP": "0"}):
                code, _, err = self.cli("maintain", action)
            self.assertEqual(code, 2)
            self.assertIn("OBSERVATORY_SYSTEM_SETUP=1", err)

    def test_doctor_names_an_unscheduled_job(self):
        # Probing is allowed here as on a person's machine; the fake answers for launchd.
        with patch.object(M, "schedule_for", return_value=FakeSchedule()), \
                patch.object(M, "_real_home", return_value=True):
            report = workspace.doctor(self.home)["maintenance"]
        self.assertTrue(report["auto_update"])
        self.assertTrue(any("not scheduled" in w for w in report["warnings"]))


def backup_vault_store():
    return vault


# --- a database that is not the workspace's own never reaches its backups root ---------

class ForeignDatabase(Base):
    def test_a_copy_of_another_database_stays_beside_it(self):
        # Seen on a maintainer's machine: three tiny copies of a database named by
        # OBSERVATORY_DB, under the workspace's label, pushed the real daily copies out.
        vault.ensure_passphrase(self.home, self.store)
        other = self.base / "elsewhere" / "other.db"
        other.parent.mkdir()
        import sqlite3
        with sqlite3.connect(other) as conn:
            conn.execute("CREATE TABLE t (x)")
        env = {**os.environ, "OBSERVATORY_DB": str(other)}
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "backup_store.py")], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("is not this workspace's store", out.stderr)
        root = vault.root_info(self.home)["path"]
        self.assertEqual(list(root.glob("observatory-db-*")) if root.exists() else [], [])
        self.assertTrue(any(p.name.startswith("observatory.db.backup-") or p.name.startswith("other.db")
                            for p in other.parent.iterdir()))
        own = subprocess.run([sys.executable, str(ROOT / "tools" / "backup_store.py")],
                             env={k: v for k, v in os.environ.items() if k != "OBSERVATORY_DB"},
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(own.returncode, 0, own.stderr)
        self.assertEqual(len(list(root.glob("observatory-db-*.obsdb"))), 1, "the workspace's own store still goes there")


class ConcurrentWriter(Base):
    def test_a_copy_taken_while_another_process_writes_is_never_torn(self):
        # OBS-37: an immutable reader tore every copy of a store a session server wrote to.
        import sqlite3
        from store import compatibility
        db = self.base / "busy.db"
        with sqlite3.connect(db) as w:
            w.execute("PRAGMA journal_mode=WAL")
            w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x)")
            w.execute("CREATE INDEX ix ON t (x)")
            w.executemany("INSERT INTO t (x) VALUES (?)", [(os.urandom(300),) for _ in range(20000)])
        writer = subprocess.Popen([sys.executable, "-c", (
            "import sqlite3, os, time\n"
            f"c = sqlite3.connect({str(db)!r}, timeout=30)\n"
            "end = time.time() + 30\n"
            "while time.time() < end:\n"
            "    c.execute('INSERT INTO t (x) VALUES (?)', (os.urandom(300),))\n"
            "    c.execute('DELETE FROM t WHERE id IN (SELECT id FROM t ORDER BY random() LIMIT 1)')\n"
            "    c.commit()\n"
            "    if os.urandom(1)[0] < 40: c.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n")])
        self.addCleanup(writer.wait, timeout=30)
        self.addCleanup(writer.kill)
        time.sleep(1)
        self.assertTrue(Path(str(db) + "-shm").exists(), "the writer keeps a -shm, as live servers do")
        self.assertNotIn("immutable", compatibility.readonly_uri(db))
        torn = 0
        for i in range(6):
            out = self.base / f"copy-{i}.db"
            src = sqlite3.connect(compatibility.readonly_uri(db), uri=True, timeout=30)
            dst = sqlite3.connect(out)
            src.backup(dst)
            src.close()
            torn += dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            dst.close()
        self.assertEqual(torn, 0)

    def test_a_writer_that_arrives_after_the_reader_chose_immutable_never_tears_the_snapshot(self):
        # Audit A24: the test above starts its writer first, so `-shm` is already there and
        # the reader takes locks. The other half of OBS-37 is the idle store — no `-shm`,
        # so the reader is opened `immutable` and takes no locks — and a session server
        # that opens it and checkpoints WHILE the copy runs. An immutable reader tears
        # such a copy (measured: 6 of 6 raw copies of this fixture failed integrity_check);
        # the snapshot's own copy must still come out whole.
        import sqlite3
        from store import compatibility
        db = self.base / "idle.db"
        with contextlib.closing(sqlite3.connect(db)) as w:
            w.execute("PRAGMA journal_mode=WAL")
            w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, x)")
            w.execute("CREATE INDEX ix ON t (x)")
            w.executemany("INSERT INTO t (x) VALUES (?)", [(os.urandom(300),) for _ in range(60000)])
            w.commit()
        self.assertFalse(Path(str(db) + "-shm").exists(), "an idle store: the last connection closed")
        real, uris, writers = compatibility.readonly_uri, [], []

        def reader_then_writer(target):
            # The uri is chosen first, as copy_database does; only then does the writer come.
            uri = real(target)
            uris.append(uri)
            if not writers:
                writers.append(subprocess.Popen([sys.executable, "-c", (
                    "import sqlite3, os, time\n"
                    f"c = sqlite3.connect({str(db)!r}, timeout=30)\n"
                    "end = time.time() + 30\n"
                    "while time.time() < end:\n"
                    "    c.execute('INSERT INTO t (x) VALUES (?)', (os.urandom(300),))\n"
                    "    c.execute('DELETE FROM t WHERE id IN (SELECT id FROM t ORDER BY random() LIMIT 1)')\n"
                    "    c.commit()\n"
                    "    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n")]))
                self.addCleanup(writers[0].wait, timeout=30)
                self.addCleanup(writers[0].kill)
                deadline = time.monotonic() + 30
                while not Path(str(db) + "-shm").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.3)  # the writer is committing and checkpointing by now
            return uri
        out = self.base / "snapshot.db"
        with patch.object(compatibility, "readonly_uri", reader_then_writer):
            workspace_upgrade.copy_database(db, out)
        self.assertIn("immutable=1", uris[0], "the case under test: the reader was opened immutable")
        with contextlib.closing(sqlite3.connect(out)) as copy:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertGreater(copy.execute("SELECT count(*) FROM t").fetchone()[0], 59000)

    def test_a_quiet_database_is_still_read_without_creating_files(self):
        import sqlite3
        from store import compatibility
        db = self.base / "quiet.db"
        w = sqlite3.connect(db)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE t (x)")
        w.commit()
        w.close()  # the last connection checkpoints and removes -wal and -shm
        self.assertFalse(Path(str(db) + "-shm").exists())
        self.assertIn("immutable=1", compatibility.readonly_uri(db))
        before = sorted(p.name for p in self.base.iterdir())
        with contextlib.closing(sqlite3.connect(compatibility.readonly_uri(db), uri=True)) as conn:
            conn.execute("SELECT count(*) FROM t").fetchone()
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), before)


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

    def test_backups_of_two_workspaces_are_listed_not_chosen(self):
        # Audit A06: the empty workspace a failed reinstall started takes daily backups too,
        # and its newest one must not win over the real data.
        self.backed_up()
        own = vault.root_info(self.home)["path"]
        second = own.parent / "private workspace-0badc0de"
        shutil.copytree(own, second)
        shutil.rmtree(self.home)
        out = M.restore_latest(self.home, store=self.store)
        self.assertEqual(out["status"], "several-workspaces")
        self.assertEqual({b["label"] for b in out["backups"]}, {own.name, second.name})
        self.assertIn("--label", out["next"])
        self.assertFalse(self.home.exists() and any(self.home.iterdir()))
        out = M.restore_latest(self.home, store=self.store, label=own.name)
        self.assertEqual((out["status"], out["label"]), ("restored", own.name))

    def test_the_locked_hint_is_a_command_a_person_can_run(self):
        # Audit A05: init has filled the home by then, and a passphrase never goes in argv.
        self.backed_up()
        shutil.rmtree(self.home)
        out = M.restore_latest(self.home, store=FakeStore())
        self.assertEqual(out["status"], "backups-found-locked")
        self.assertIn("--home NEW_EMPTY_HOME full restore", out["next"])
        self.assertNotIn("PASSPHRASE", out["next"].upper().replace("ASKS FOR THE PASSPHRASE", ""))

    def test_a_backup_taken_before_a_passphrase_change_still_restores(self):
        self.backed_up()
        label = vault._workspace_label(self.home)
        vault.set_passphrase(self.home, "a later phrase, set after the backup was taken")
        vault.ensure_passphrase(self.home, self.store)
        shutil.rmtree(self.home)
        self.assertEqual(M.restore_latest(self.home, store=self.store)["status"], "restored")
        self.assertIn(label + vault.PREVIOUS, self.store.items)

    def test_a_rolled_back_updates_plaintext_copy_is_named(self):
        # Audit A18: `full update` keeps the changed workspace beside the restored one.
        copy = self.home.parent / f"{self.home.name}.failed-update-20261005T120000Z"
        (copy / "secrets").mkdir(parents=True)
        status = vault.status(self.home)
        self.assertTrue(any("rolled-back update" in w and copy.name in w for w in status["warnings"]),
                        status["warnings"])
        self.assertIn("before-update", status["artifacts"])

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
        return self.teams.get(app.name if app.name != A.APP_NAME else str(app.parent.name), A.TEAM_ID)

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

    def unregister(self, app):
        # Recorded with the path, and only while the bundle still exists: forgetting a
        # bundle after its folder is gone is what left a stale record (2026-10-06).
        self.calls.append(("unregister", str(app), app.is_dir()))


class FakeFetcher:
    def __init__(self, version, zipped=b"synthetic zip bytes", digest_ok=True, signer=SIGNER):
        self.version, self.zipped = version, zipped
        name = A.ZIP.format(version=version)
        self.sums = f"{hashlib.sha256(zipped).hexdigest()}  {name}\n".encode()
        self.digest_ok = digest_ok
        self.signature = signer.sign(self.sums).encode() if signer else None

    def get_json(self, url):
        name = A.ZIP.format(version=self.version)
        zdigest = hashlib.sha256(self.zipped if self.digest_ok else b"other").hexdigest()
        return {"tag_name": f"v{self.version}", "assets": [
            {"name": name, "browser_download_url": "https://example.invalid/app.zip",
             "digest": f"sha256:{zdigest}", "size": len(self.zipped)},
            {"name": "SHA256SUMS", "browser_download_url": "https://example.invalid/SHA256SUMS",
             "digest": f"sha256:{hashlib.sha256(self.sums).hexdigest()}", "size": len(self.sums)}]
            + ([{"name": "SHA256SUMS.asc", "browser_download_url": "https://example.invalid/SHA256SUMS.asc",
                 "digest": f"sha256:{hashlib.sha256(self.signature).hexdigest()}", "size": len(self.signature)}]
               if self.signature else [])}

    def fetch_file(self, url, dest, limit):
        data = (self.sums if url.endswith("SHA256SUMS") else self.signature if url.endswith(".asc")
                else self.zipped)
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), len(data)


class App(Base):
    def setUp(self):
        super().setUp()
        pinned = patch.object(release_signature, "PINNED", SIGNER.pinned())
        pinned.start()
        self.addCleanup(pinned.stop)

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

    def test_only_this_users_running_app_is_waited_for(self):
        # Audit A22: another account's open app is not ours to wait for.
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "")
        self.assertFalse(A.System(runner=runner).running())
        self.assertEqual(seen[0], ["pgrep", "-u", str(os.getuid()), "-x", A.PROCESS])

    def test_an_older_app_is_replaced_and_the_previous_kept(self):
        updater, app = self.updater()
        out = self.step(updater)
        self.assertEqual((out["result"], out["from"], out["version"]), ("updated", "0.1.0", "9.9.9"))
        self.assertEqual(A.bundle_info(app)["version"], "9.9.9")
        self.assertEqual(A.bundle_info(self.home / "store/app-previous" / A.APP_NAME)["version"], "0.1.0")
        self.assertFalse((self.home / "store/app-update").exists())
        self.assertIn(("register", A.APP_NAME), updater.system.calls)
        self.assertEqual(self.step(updater)["result"], "current")

    def test_every_bundle_is_forgotten_before_its_folder_is_deleted(self):
        # 2026-10-06: a staged NEWER bundle, deleted while still registered, stayed macOS's
        # record of the app, and the Dock drew a blank icon for the installed one.
        system = FakeAppSystem(running=True)
        updater, app = self.updater(system=system)
        self.step(updater)                       # staged, waiting for the app to quit
        staged = self.home / "store/app-update/9.9.9" / A.APP_NAME
        self.assertTrue(staged.is_dir())
        system.is_running = False
        updater.fetcher = None
        self.assertEqual(self.step(updater)["result"], "updated")
        gone = [(path, existed) for kind, path, existed in
                (c for c in system.calls if c[0] == "unregister")]
        self.assertIn((str(staged), True), gone, "the staged bundle is forgotten, while it still exists")
        self.assertTrue(any(path.endswith(f".{A.APP_NAME}.previous") and existed for path, existed in gone),
                        "and the retired one beside /Applications")
        self.assertFalse(any(not existed for _, existed in gone), gone)
        self.assertFalse((self.home / "store/app-update").exists())

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
        system.team = lambda app: A.TEAM_ID if "Applications" in str(app) and ".installing" not in str(app) \
            and "app-update" not in str(app) else "SOMEONE-ELSE"
        updater, app = self.updater(system=system)
        out = self.step(updater)
        self.assertEqual(out["result"], "refused")
        self.assertIn("not signed by the organization's team", out["detail"])
        self.assertEqual(A.bundle_info(app)["version"], "0.1.0")

    def test_the_team_is_the_pinned_one_never_whatever_signed_the_installed_app(self):
        # LC-16: an installed copy signed by another team does not make that team trusted.
        self.assertEqual(A.TEAM_ID, "KJ35UYYL22")
        system = FakeAppSystem()
        system.team = lambda app: "OTHERTEAM1"
        updater, app = self.updater(system=system)
        out = self.step(updater)
        self.assertEqual(out["result"], "refused")
        self.assertIn(A.TEAM_ID, out["detail"])
        self.assertEqual(A.bundle_info(app)["version"], "0.1.0")
        # The organization's bundle replaces an installed app another team signed.
        shutil.rmtree(self.base / "Applications")
        system = FakeAppSystem()
        system.team = lambda app: "OTHERTEAM1" if "Applications" in str(app) and ".installing" not in str(app) \
            else A.TEAM_ID
        updater, app = self.updater(system=system)
        self.assertEqual(self.step(updater)["result"], "updated")

    def test_gatekeeper_refusal_digest_mismatch_and_wrong_bundles_are_refused(self):
        for system, fetcher, why in (
                (FakeAppSystem(refuse="Gatekeeper refused the bundle: rejected"), None, "Gatekeeper"),
                (FakeAppSystem(), FakeFetcher("9.9.9", digest_ok=False), "digest"),
                (FakeAppSystem(), FakeFetcher("9.9.9", signer=None), "unsigned release"),
                (FakeAppSystem(), FakeFetcher("9.9.9", signer=pgp_fixture.Key()), "release key"),
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

    def test_sessions_starting_together_after_the_window_start_one_bridge(self):
        # Audit A20: the old claim removed and re-made a stale lock, so two sessions could
        # both see it stale and both win. The check and the stamp are one locked step now.
        root, log = self.fake_install(with_maintain=False)
        state = self.base / "state" / "project-observatory"
        state.mkdir(parents=True)
        stamp = state / "update-bridge.at"
        stamp.write_text("")
        old = time.time() - 2 * 86400
        os.utime(stamp, (old, old))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.base / "user"), "OBSERVATORY_ROOT": str(root),
               "OBSERVATORY_HOME": str(self.home), "XDG_STATE_HOME": str(self.base / "state")}
        procs = [subprocess.Popen(["/bin/bash", str(HOOK)], stdin=subprocess.DEVNULL, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(8)]
        for proc in procs:
            proc.wait(timeout=30)
        self.wait_for(log, "--apply")
        time.sleep(1.5)
        self.assertEqual(log.read_text().count("--check"), 1)
        self.assertGreater(stamp.stat().st_mtime, old + 86400, "the window starts again")

    def test_the_bridge_says_there_is_nothing_to_stop_where_launchd_is_absent(self):
        # Audit A02: without launchd, `--apply` alone refuses every day.
        root, log = self.fake_install(with_maintain=False)
        self.run_hook(root)
        text = self.wait_for(log, "--apply")
        if shutil.which("launchctl", path="/usr/bin:/bin"):
            self.assertIn("po full update --apply\n", text)
        else:
            self.assertIn("po full update --apply --writers-stopped", text)

    def test_a_new_engine_gets_its_maintenance_hook_with_the_setup_flag(self):
        root, log = self.fake_install(with_maintain=True)
        self.run_hook(root)
        text = self.wait_for(log, "maintain hook")
        self.assertIn("maintain hook setup=1", text)
        self.assertNotIn("po full update", text)


if __name__ == "__main__":
    unittest.main()
