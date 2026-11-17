#!/usr/bin/env python3
"""OBS-563, CP-20: the configuration door.

Every setting the engine reads is a declared entry with a type, a default, its file and its gate. One door previews a
change (`plan`), applies exactly the previewed change (`apply DIGEST`, refused `plan_stale` when a file moved since),
journals it with the values before and after, and undoes it (`undo ID`, refused when the files no longer hold what it
wrote). An unknown key, a value of the wrong type and anything shaped like a credential are refused by type and never
echoed. The existing `configure SECTION NAME VALUE` keeps its answers and writes through the same door.
"""
from __future__ import annotations
import contextlib
import importlib
import io
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

FAKE_KEY = "sk-or-v1-" + "FAKE" * 10


class Door(unittest.TestCase):
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
        import configuration_door
        self.door = importlib.reload(configuration_door)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------------

    def read(self, name: str) -> dict:
        return json.loads((self.home / "config" / name).read_text(encoding="utf-8"))

    def journal(self) -> list[dict]:
        file = self.home / "store" / "logs" / "configure.jsonl"
        if not file.exists():
            return []
        return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def refused(self, code: str, call, *args, **kwargs) -> dict:
        with self.assertRaises(self.door.Refusal) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.answer())
        return caught.exception.answer()

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = workspace.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    # --- the catalogue -------------------------------------------------------------

    def test_the_catalogue_lists_every_key_with_its_type_default_file_gate_and_value(self):
        listing = self.door.listing(self.home)
        self.assertEqual(listing["degraded"], [])
        rows = {row["key"]: row for row in listing["settings"]}
        for name in config.known_names("integrations"):
            self.assertIn(f"integrations.{name}", rows)
        for name in config.known_names("features"):
            self.assertIn(f"features.{name}", rows)
        for key in ("sources.projects", "interface.locale", "storage.backups", "learning_review.settle_hour_utc",
                    "model.chain", "budget.daily_ceiling", "apps.location", "loopback.peerCheck",
                    "guard.levels.low.free_gb", "guard.levels.critical.free_gb", "guard.temp_days.normal",
                    "cleanup.auto.merged_branch_idle_days"):
            self.assertIn(key, rows)
        for row in rows.values():
            self.assertTrue({"key", "type", "default", "file", "description", "gate", "value"} <= set(row), row)
            self.assertTrue(row["description"], row["key"])
        self.assertEqual(rows["features.auto_cleanup"]["type"], "bool")
        self.assertEqual(rows["features.auto_cleanup"]["gate"], {"person": "on"})
        self.assertEqual(rows["interface.locale"]["choices"], ["en", "ru"])
        self.assertEqual((rows["guard.levels.low.free_gb"]["default"], rows["guard.levels.low.free_gb"]["value"]),
                         (25, 25))
        self.assertEqual(rows["guard.levels.low.free_gb"]["file"], "config/cleanup.json")
        self.assertEqual(rows["learning_review.settle_hour_utc"]["value"], 6)
        # The watches upcoming modules read are on by default, and `enabled` agrees.
        for key in ("integrations.vulnerabilities", "integrations.github_issues"):
            self.assertIs(rows[key]["default"], True)
            self.assertIs(rows[key]["value"], True)
        settings = config.load(self.home)
        self.assertTrue(config.enabled("vulnerabilities", settings=settings, environ={}))
        self.assertFalse(config.enabled("github", settings=settings, environ={}))

    def test_an_unreadable_file_is_degraded_never_a_default(self):
        (self.home / "config" / "cleanup.json").write_text("{not json", encoding="utf-8")
        listing = self.door.listing(self.home)
        self.assertTrue(any(d.get("source") == "config/cleanup.json" for d in listing["degraded"]), listing["degraded"])
        rows = {row["key"]: row for row in listing["settings"]}
        self.assertIsNone(rows["guard.levels.low.free_gb"]["value"])

    # --- plan, apply, undo ---------------------------------------------------------

    def test_plan_apply_and_undo_round_trip(self):
        before = {name: self.read(name) for name in ("settings.json", "models.json", "cleanup.json")}
        plan = self.door.plan({"integrations.github": True, "guard.temp_days.normal": 5,
                               "budget.daily_ceiling": 2.5, "interface.locale": "ru"}, base=self.home)
        self.assertTrue(plan["digest"].startswith("sha256:"))
        self.assertEqual(plan["before"]["integrations.github"], False)
        self.assertEqual(plan["after"], {"integrations.github": True, "guard.temp_days.normal": 5,
                                         "budget.daily_ceiling": 2.5, "interface.locale": "ru"})
        self.assertEqual(plan["degraded"], [])
        self.assertTrue(plan["effects"])
        # Raising a budget and turning on an integration need the person; keeping temporary files longer does not.
        self.assertEqual(sorted(plan["needsPerson"]), ["budget.daily_ceiling", "integrations.github"])
        self.assertEqual({name: self.read(name) for name in before}, before, "a plan writes no configuration")

        done = self.door.apply(plan["digest"], actor="person:cli", base=self.home)
        self.assertEqual(done["status"], "applied")
        self.assertIs(self.read("settings.json")["integrations"]["github"], True)
        self.assertEqual(self.read("settings.json")["interface"]["locale"], "ru")
        self.assertEqual(self.read("models.json")["wallet"]["daily_ceiling"], 2.5)
        self.assertEqual(self.read("cleanup.json")["guard"]["temp_days"]["normal"], 5)
        line = self.journal()[-1]
        self.assertEqual((line["id"], line["kind"], line["actor"], line["digest"]),
                         (done["journal"], "apply", "person:cli", plan["digest"]))
        by_key = {c["key"]: c for c in line["changes"]}
        self.assertEqual((by_key["integrations.github"]["before"], by_key["integrations.github"]["after"]),
                         (False, True))
        self.assertEqual(by_key["guard.temp_days.normal"]["before"], 3)
        # The same digest twice is not a second write.
        self.refused("plan_unknown", self.door.apply, plan["digest"], actor="person:cli", base=self.home)

        undone = self.door.undo(done["journal"], actor="person:cli", base=self.home)
        self.assertEqual(undone["status"], "undone")
        self.assertEqual({name: self.read(name) for name in before}, before)
        self.assertEqual(self.journal()[-1]["undoes"], done["journal"])
        self.refused("already_undone", self.door.undo, done["journal"], actor="person:cli", base=self.home)
        self.refused("journal_unknown", self.door.undo, "cfg_" + "0" * 16, actor="person:cli", base=self.home)

    def test_a_plan_whose_files_moved_is_stale_and_writes_nothing(self):
        plan = self.door.plan({"integrations.github": True}, base=self.home)
        code, _, _ = self.cli("configure", "features", "notifications", "true")
        self.assertEqual(code, 0)
        written = self.read("settings.json")
        self.refused("plan_stale", self.door.apply, plan["digest"], actor="person:cli", base=self.home)
        self.assertEqual(self.read("settings.json"), written)
        self.assertNotIn("github", written["integrations"])

    def test_undo_is_refused_when_the_file_no_longer_holds_what_it_wrote(self):
        done = self.door.apply(self.door.plan({"features.notifications": True}, base=self.home)["digest"],
                               actor="person:cli", base=self.home)
        self.cli("configure", "features", "notifications", "false")
        answer = self.refused("undo_refused", self.door.undo, done["journal"], actor="person:cli", base=self.home)
        self.assertIn("features.notifications", answer["keys"])
        self.assertIs(self.read("settings.json")["features"]["notifications"], False)

    def test_back_to_the_default_and_local_settings(self):
        done = self.door.apply(self.door.plan({"apps.location": "user", "storage.backups": str(self.base / "bk")},
                                              base=self.home)["digest"], actor="person:cli", base=self.home)
        local = json.loads(config.local_path(self.home).read_text())
        self.assertEqual((local["apps"], local["storage"]["backups"]), ({"location": "user"}, str(self.base / "bk")))
        self.door.undo(done["journal"], actor="person:cli", base=self.home)
        local = json.loads(config.local_path(self.home).read_text())
        self.assertNotIn("location", local.get("apps") or {})
        self.assertNotIn("backups", local.get("storage") or {})
        self.assertNotIn("backups", config.load(self.home).get("storage") or {})
        self.door.apply(self.door.plan({"interface.locale": "ru"}, base=self.home)["digest"], actor="person:cli",
                        base=self.home)
        plan = self.door.plan({"interface.locale": None}, base=self.home)
        self.assertEqual(plan["after"]["interface.locale"], "en")
        self.door.apply(plan["digest"], actor="person:cli", base=self.home)
        self.assertNotIn("locale", self.read("settings.json").get("interface") or {})

    # --- refusals ----------------------------------------------------------------

    def test_unknown_keys_and_invalid_values_are_refused_by_type(self):
        self.refused("unknown_key", self.door.plan, {"integrations.colours": True}, base=self.home)
        self.refused("unknown_key", self.door.plan, {"nothing": 1}, base=self.home)
        for change in ({"integrations.github": "yes"}, {"learning_review.settle_hour_utc": 24},
                       {"learning_review.settle_hour_utc": True}, {"interface.locale": "fr"},
                       {"sources.projects": "relative/path"}, {"budget.daily_ceiling": -1},
                       {"model.chain": ["not a model"]}, {"guard.levels.low.free_gb": "many"}):
            with self.subTest(change):
                self.refused("invalid_value", self.door.plan, change, base=self.home)
        self.refused("no_changes", self.door.plan, {}, base=self.home)
        self.assertEqual(self.journal(), [])

    def test_a_credential_shaped_key_or_value_is_refused_and_never_echoed(self):
        for change in ({"sources.projects": f"/srv/example-ws/{FAKE_KEY}"}, {"model.chain": [f"vendor/{FAKE_KEY}"]},
                       {FAKE_KEY: True}):
            with self.subTest(sorted(change)):
                answer = self.refused("secret_refused" if FAKE_KEY not in change else "unknown_key",
                                      self.door.plan, change, base=self.home)
                self.assertNotIn(FAKE_KEY, json.dumps(answer))
        self.assertEqual(self.journal(), [])
        plans = self.door._plans(self.home)
        self.assertFalse(plans.exists() and any(plans.iterdir()))

    def test_an_agent_needs_the_operators_words_for_a_person_gated_change(self):
        safe = self.door.plan({"features.notifications": True}, base=self.home)
        self.assertEqual(self.door.apply(safe["digest"], actor="agent:mcp", base=self.home)["status"], "applied")
        gated = self.door.plan({"features.auto_cleanup": True}, base=self.home)
        self.assertEqual(gated["needsPerson"], ["features.auto_cleanup"])
        answer = self.refused("operator-decision-required", self.door.apply, gated["digest"], actor="agent:mcp",
                              base=self.home)
        self.assertEqual(answer["keys"], ["features.auto_cleanup"])
        self.assertIsNot(self.read("settings.json")["features"].get("auto_cleanup"), True)
        self.refused("decision-invalid", self.door.apply, gated["digest"], actor="agent:mcp", base=self.home,
                     decision={"by": "operator", "via": "relayed-instruction", "action": "configure.apply",
                               "target": "sha256:" + "1" * 64, "instruction": "turn it on",
                               "at": self.door._now()})
        words = {"by": "operator", "via": "relayed-instruction", "action": "configure.apply",
                 "target": gated["digest"], "instruction": "turn on auto cleanup", "at": self.door._now()}
        done = self.door.apply(gated["digest"], actor="agent:mcp", base=self.home, decision=words)
        self.assertIs(self.read("settings.json")["features"]["auto_cleanup"], True)
        self.assertEqual(self.journal()[-1]["decision"]["instruction"], "turn on auto cleanup")
        # Turning it back off is safe for anyone.
        self.assertEqual(self.door.undo(done["journal"], actor="agent:mcp", base=self.home)["status"], "undone")

    # --- the command line ------------------------------------------------------------

    def test_existing_configure_answers_are_unchanged_and_journalled(self):
        code, out, _ = self.cli("configure", "integrations", "github", "true")
        self.assertEqual((code, json.loads(out)), (0, {"status": "configured", "section": "integrations",
                                                       "name": "github"}))
        self.assertIs(self.read("settings.json")["integrations"]["github"], True)
        code, out, _ = self.cli("configure", "model", "chain", "vendor/model-a,vendor/model-b")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "configured")
        self.assertEqual([e["id"] for e in self.read("models.json")["chain"]], ["vendor/model-a", "vendor/model-b"])
        code, out, _ = self.cli("configure", "apps", "location", "user")
        self.assertEqual(json.loads(out), {"status": "configured", "section": "apps", "name": "location",
                                           "file": "config/local.json"})
        code, _, err = self.cli("configure", "integrations", "colours", "true")
        self.assertEqual(code, 2)
        self.assertIn("Unknown integration", err)
        kinds = [(line["actor"], [c["key"] for c in line["changes"]]) for line in self.journal()]
        self.assertEqual(kinds, [("person:cli", ["integrations.github"]), ("person:cli", ["model.chain"]),
                                 ("person:cli", ["apps.location"])])

    def test_the_command_line_lists_plans_applies_and_undoes(self):
        code, out, _ = self.cli("configure", "--list")
        self.assertEqual(code, 0)
        self.assertIn("features.auto_cleanup", {row["key"] for row in json.loads(out)["settings"]})
        code, out, _ = self.cli("configure", "--plan", "features.auto_cleanup=true", "guard.levels.low.free_gb=30")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["after"], {"features.auto_cleanup": True, "guard.levels.low.free_gb": 30})
        code, out, _ = self.cli("configure", "--apply", plan["digest"])
        self.assertEqual(code, 0)
        journal = json.loads(out)["journal"]
        self.assertIs(self.read("settings.json")["features"]["auto_cleanup"], True)
        code, out, _ = self.cli("configure", "--undo", journal)
        self.assertEqual((code, json.loads(out)["status"]), (0, "undone"))
        code, out, _ = self.cli("configure", "--plan", "integrations.colours=true")
        self.assertEqual((code, json.loads(out)["error"]), (2, "unknown_key"))
        code, _, err = self.cli("configure")
        self.assertEqual(code, 2)
        self.assertIn("usage: project-observatory full configure", err)
        code, _, _ = self.cli("configure", "--list", "integrations", "github", "true")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
