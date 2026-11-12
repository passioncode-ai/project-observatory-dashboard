#!/usr/bin/env python3
"""OBS-353: one machine-local settings file, `config/local.json`.

What this machine and this OS account decide (the scheduler switch, the backups root, ports, the app location) lives
in one owner-only file that profiles never carry. The two settings that moved out of `config/settings.json` are
mirrored back into it on every write, so an older engine reads the same values, and a crash between the three writes
of `configuration.write_local` never loses a value.
"""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration as config
import workspace

PLACE = "m-" + "a" * 24


class Crash(Exception):
    pass


class LocalSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(self.base / "user"),
                                           "USERPROFILE": str(self.base / "user")}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        self.backups = str(self.base / "backups")
        self.set_settings({"features": {"scheduler": True}, "storage": {"backups": self.backups}})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def settings(self) -> dict:
        return json.loads((self.home / "config" / "settings.json").read_text())

    def set_settings(self, changes: dict) -> None:
        doc = self.settings()
        for section, values in changes.items():
            doc.setdefault(section, {}).update(values)
        workspace.write_json(self.home / "config" / "settings.json", doc)

    def local_doc(self) -> dict:
        return json.loads(config.local_path(self.home).read_text())

    def write(self, changes: dict, writer=None) -> dict:
        with workspace.lock(self.home):
            return config.write_local(self.home, changes, place=PLACE, writer=writer)

    def test_absent_file_reads_every_value_from_settings(self):
        self.assertFalse(config.local_path(self.home).exists())
        doc = config.load(self.home)
        self.assertIs(doc["features"]["scheduler"], True)
        self.assertEqual(doc["storage"]["backups"], self.backups)
        state = config.local(self.home, place=PLACE)
        self.assertFalse(state["present"])
        self.assertEqual(state["settings"]["features.scheduler"], {"value": True, "source": "settings-mirror"})

    def test_first_write_copies_scheduler_and_backups_then_mirrors(self):
        self.write({"scheduler": False})
        local = self.local_doc()
        self.assertEqual((local["schema"], local["place"]), (config.LOCAL_SCHEMA, PLACE))
        self.assertIs(local["scheduler"], False)
        self.assertEqual(local["storage"]["backups"], self.backups, "the move copies what it did not change")
        self.assertEqual(local["mirrored"], {"features.scheduler": False, "storage.backups": self.backups})
        self.assertIs(self.settings()["features"]["scheduler"], False, "mirrored into settings.json")
        self.assertIs(config.load(self.home)["features"]["scheduler"], False)
        self.assertEqual(oct(config.local_path(self.home).stat().st_mode & 0o777), oct(0o600))

    def test_crash_after_local_before_settings_keeps_the_new_value(self):
        calls = []

        def writer(path, doc):
            calls.append(path.name)
            if len(calls) == 2:
                raise Crash()
            config_atomic(path, doc)
        with self.assertRaises(Crash):
            self.write({"scheduler": False}, writer=writer)
        self.assertIs(self.settings()["features"]["scheduler"], True, "settings.json was never written")
        self.assertIs(config.load(self.home)["features"]["scheduler"], False, "local.json, new value, wins")

    def test_crash_after_settings_before_mirror_adopts_the_same_value(self):
        calls = []

        def writer(path, doc):
            calls.append(path.name)
            if len(calls) == 3:
                raise Crash()
            config_atomic(path, doc)
        with self.assertRaises(Crash):
            self.write({"scheduler": False}, writer=writer)
        self.assertIs(self.settings()["features"]["scheduler"], False)
        self.assertIs(config.load(self.home)["features"]["scheduler"], False)
        self.write({})
        self.assertEqual(self.local_doc()["mirrored"]["features.scheduler"], False)

    def test_a_hand_edit_of_settings_is_adopted_and_named(self):
        self.write({"scheduler": False})
        self.set_settings({"features": {"scheduler": True}})            # an older engine, or a hand edit
        self.assertIs(config.load(self.home)["features"]["scheduler"], True, "the later write wins")
        state = config.local(self.home, place=PLACE)
        self.assertEqual(state["settings"]["features.scheduler"]["source"], "adopted")
        self.assertIn("local-settings-adopted", [n["code"] for n in state["notes"]])
        self.write({})
        self.assertIs(self.local_doc()["scheduler"], True, "the next write records it")
        self.assertEqual(config.local(self.home, place=PLACE)["settings"]["features.scheduler"]["source"], "local")

    def test_an_older_reader_sees_the_mirrored_values(self):
        self.write({"scheduler": False, "storage": {"backups": str(self.base / "elsewhere")}})
        raw = self.settings()                                            # what an engine without local.json reads
        self.assertIs(raw["features"]["scheduler"], False)
        self.assertEqual(raw["storage"]["backups"], str(self.base / "elsewhere"))
        self.assertEqual(raw["schema_version"], config.CONFIG_VERSION)
        self.assertEqual(raw.get("must_understand", []), [], "nothing an older reader would refuse")

    def test_stale_place_ignores_ports_and_apps(self):
        self.write({"ports": {"scheme": "legacy", "server": 47311}, "apps": {"location": "user"}})
        here = config.local(self.home, place=PLACE)
        self.assertEqual(here["settings"]["apps.location"], {"value": "user", "source": "local"})
        other = config.local(self.home, place="m-" + "b" * 24)
        self.assertFalse(other["place"]["matches"])
        self.assertEqual(other["settings"]["ports"], {"value": None, "source": "default"})
        self.assertEqual(other["settings"]["apps.location"], {"value": "auto", "source": "default"})
        self.assertIn("local-settings-stale", [n["code"] for n in other["notes"]])
        self.assertIs(config.load(self.home)["features"]["scheduler"], True, "scheduler and storage still read")
        self.assertTrue(config.local(self.home, place=PLACE + ".a-0123456789")["place"]["matches"],
                        "a bare host id matches this host's place")

    def test_unknown_schema_is_refused_by_name(self):
        self.write({"scheduler": False})
        doc = self.local_doc()
        doc["schema"] = "observatory-local/2"
        workspace.write_json(config.local_path(self.home), doc)
        state = config.local(self.home, place=PLACE)
        self.assertEqual([n["code"] for n in state["notes"]], ["local-settings-unsupported"])
        self.assertIs(config.load(self.home)["features"]["scheduler"], False, "settings.json, mirrored, still reads")

    @unittest.skipUnless(hasattr(os, "symlink") and hasattr(os, "getuid"), "POSIX links and modes")
    def test_link_and_loose_mode_are_refused(self):
        self.write({"scheduler": False})
        file = config.local_path(self.home)
        os.chmod(file, 0o644)
        self.assertEqual([n["code"] for n in config.local(self.home, place=PLACE)["notes"]],
                         ["local-settings-unreadable"])
        os.chmod(file, 0o600)
        real = self.base / "elsewhere.json"
        file.rename(real)
        file.symlink_to(real)
        self.assertEqual([n["code"] for n in config.local(self.home, place=PLACE)["notes"]],
                         ["local-settings-unreadable"])
        self.assertIs(config.load(self.home)["features"]["scheduler"], False, "load falls back to settings.json")

    def test_unknown_and_reserved_keys_are_kept_and_listed(self):
        self.write({"scheduler": False})
        doc = self.local_doc()
        doc.update({"context": "personal", "fromANewerEngine": {"x": 1}})
        workspace.write_json(config.local_path(self.home), doc)
        state = config.local(self.home, place=PLACE)
        self.assertEqual((state["reserved"], state["unknown"]), (["context"], ["fromANewerEngine"]))
        self.write({"apps": {"location": "system"}})
        kept = self.local_doc()
        self.assertEqual((kept["context"], kept["fromANewerEngine"]), ("personal", {"x": 1}))

    def test_configure_writes_the_moved_keys_and_the_new_sections(self):
        for argv in (["configure", "features", "scheduler", "false"], ["configure", "apps", "location", "user"],
                     ["configure", "loopback", "peerCheck", "always"]):
            with patch("sys.stdout"):
                self.assertEqual(workspace.main(argv), 0, argv)
        local = self.local_doc()
        self.assertEqual((local["scheduler"], local["apps"], local["loopback"]),
                         (False, {"location": "user"}, {"peerCheck": "always"}))
        self.assertIs(self.settings()["features"]["scheduler"], False)
        self.assertNotIn("apps", self.settings())
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertNotEqual(workspace.main(["configure", "apps", "location", "elsewhere"]), 0)


def config_atomic(path, doc):
    import atomic
    atomic.write_json(path, doc, indent=2)


if __name__ == "__main__":
    unittest.main()
