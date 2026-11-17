#!/usr/bin/env python3
"""OBS-563, CP-20: the configuration door.

Every setting the engine reads is a declared entry with a type, a default, its file and its gate. One door previews a
change (`plan`), applies exactly the previewed change (`apply DIGEST`, refused `plan-stale` when a file moved since),
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
        self.refused("plan-unknown", self.door.apply, plan["digest"], actor="person:cli", base=self.home)

        undone = self.door.undo(done["journal"], actor="person:cli", base=self.home)
        self.assertEqual(undone["status"], "undone")
        self.assertEqual({name: self.read(name) for name in before}, before)
        self.assertEqual(self.journal()[-1]["undoes"], done["journal"])
        self.refused("already-undone", self.door.undo, done["journal"], actor="person:cli", base=self.home)
        self.refused("journal-unknown", self.door.undo, "cfg_" + "0" * 16, actor="person:cli", base=self.home)

    def test_a_plan_whose_files_moved_is_stale_and_writes_nothing(self):
        plan = self.door.plan({"integrations.github": True}, base=self.home)
        code, _, _ = self.cli("configure", "features", "notifications", "true")
        self.assertEqual(code, 0)
        written = self.read("settings.json")
        self.refused("plan-stale", self.door.apply, plan["digest"], actor="person:cli", base=self.home)
        self.assertEqual(self.read("settings.json"), written)
        self.assertNotIn("github", written["integrations"])

    def test_undo_is_refused_when_the_file_no_longer_holds_what_it_wrote(self):
        done = self.door.apply(self.door.plan({"features.notifications": True}, base=self.home)["digest"],
                               actor="person:cli", base=self.home)
        self.cli("configure", "features", "notifications", "false")
        answer = self.refused("undo-refused", self.door.undo, done["journal"], actor="person:cli", base=self.home)
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

    def test_every_refusal_is_answered_in_the_contracts_spelling_and_the_earlier_one_is_read(self):
        """OBS-613: the door answered `plan_stale` beside every other tool's `invalid-argument`. It answers the
        contract's hyphenated spelling only, and a code it is handed in the earlier spelling reads as the same."""
        for earlier in ("unknown_key", "invalid_value", "secret_refused", "no_changes", "file_unreadable",
                        "plan_stale", "plan_unknown", "journal_unknown", "already_undone", "undo_refused",
                        "actor_refused", "invalid_argument", "workspace_refused", "operator_decision_required",
                        "decision_invalid"):
            with self.subTest(earlier):
                now = earlier.replace("_", "-")
                self.assertEqual(self.door.refusal_code(earlier), now)
                self.assertEqual(self.door.refusal_code(now), now)
                self.assertEqual(self.door.Refusal(earlier, "why").answer()["error"], now)
        self.assertEqual(self.door.refusal_code("some_other_word"), "some_other_word")

    def test_unknown_keys_and_invalid_values_are_refused_by_type(self):
        self.refused("unknown-key", self.door.plan, {"integrations.colours": True}, base=self.home)
        self.refused("unknown-key", self.door.plan, {"nothing": 1}, base=self.home)
        for change in ({"integrations.github": "yes"}, {"learning_review.settle_hour_utc": 24},
                       {"learning_review.settle_hour_utc": True}, {"interface.locale": "fr"},
                       {"sources.projects": "relative/path"}, {"budget.daily_ceiling": -1},
                       {"model.chain": ["not a model"]}, {"guard.levels.low.free_gb": "many"}):
            with self.subTest(change):
                self.refused("invalid-value", self.door.plan, change, base=self.home)
        self.refused("no-changes", self.door.plan, {}, base=self.home)
        self.assertEqual(self.journal(), [])

    def test_a_credential_shaped_key_or_value_is_refused_and_never_echoed(self):
        for change in ({"sources.projects": f"/srv/example-ws/{FAKE_KEY}"}, {"model.chain": [f"vendor/{FAKE_KEY}"]},
                       {FAKE_KEY: True}):
            with self.subTest(sorted(change)):
                answer = self.refused("secret-refused" if FAKE_KEY not in change else "unknown-key",
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
        self.assertEqual((code, json.loads(out)["error"]), (2, "unknown-key"))
        code, _, err = self.cli("configure")
        self.assertEqual(code, 2)
        self.assertIn("usage: project-observatory full configure", err)
        code, _, _ = self.cli("configure", "--list", "integrations", "github", "true")
        self.assertEqual(code, 2)



class EverySettingThroughTheDoor(Door):
    """OBS-612, CP-20: the settings that lived outside the door (the two switch files, the meter, retention, the
    machine watch, the review policy, the machine backup, the relay, the tracked folders and the organizations) are
    declared keys with a type, a default, their file and their gate. Their files keep their names and formats, so an
    existing install reads the same values; readers of a value the door owns read it through the door."""

    KEYS = {
        "updates.auto": ("auto-update", "bool", True, "always"),
        "analytics.send": ("analytics", "bool", True, "on"),
        "meter.estateMonthUsd": ("config/meter.json", "number", 1000.0, "raise"),
        "meter.issuer": ("config/meter.json", "text", None, "set"),
        "meter.slotsRequireGrant": ("config/meter.json", "enum", "when-token", "values"),
        "retention.events_days": ("config/retention.json", "int", 365, "lower"),
        "retention.ledger.proposed_days": ("config/retention.json", "int", 90, "lower"),
        "retention.ledger.never": ("config/retention.json", "json", None, "always"),
        "machine.every_hours": ("config/machine.json", "int", 12, "never"),
        "machine.locations": ("config/machine.json", "json", None, "never"),
        "review.remind.count": ("config/review-policy.json", "int", 20, "never"),
        "review.rules.registry.resources-append": ("config/review-policy.json", "bool", True, "on"),
        "machine_backup.enabled": ("config/machine-backup.json", "bool", True, "off"),
        "machine_backup.transcripts": ("config/machine-backup.json", "bool", False, "on"),
        "machine_backup.limits.total_mb": ("config/machine-backup.json", "int", 320, "never"),
        "relay.enabled": ("config/relay.json", "bool", False, "on"),
        "relay.port": ("config/relay.json", "int", None, "never"),
        "tracked.local_folders": ("config/tracked.json", "json", [], "never"),
        "organizations.organizations": ("config/organizations.json", "json", {}, "always"),
    }

    def rows(self) -> dict:
        listing = self.door.listing(self.home)
        self.assertEqual(listing["degraded"], [])
        return {row["key"]: row for row in listing["settings"]}

    def apply(self, changes: dict, actor: str = "person:cli") -> dict:
        return self.door.apply(self.door.plan(changes, base=self.home)["digest"], actor=actor, base=self.home)

    def test_each_file_is_declared_with_type_default_file_and_gate(self):
        rows = self.rows()
        for key, (file, kind, default, person) in self.KEYS.items():
            with self.subTest(key):
                self.assertEqual((rows[key]["file"], rows[key]["type"], rows[key]["gate"]["person"]),
                                 (file, kind, person))
                if default is not None:
                    self.assertEqual((rows[key]["default"], rows[key]["value"]), (default, default))
        self.assertEqual(rows["machine.locations"]["value"][0]["kind"], "cache")
        self.assertEqual(rows["retention.ledger.never"]["value"], ["supported", "contested", "stale", "superseded",
                                                                   "archived"])
        # Never a credential: no declared key holds a secret's value, and a credential-shaped value is refused.
        for change in ({"meter.issuer": FAKE_KEY}, {"relay.origin": f"https://{FAKE_KEY}.example"},
                       {"tracked.local_folders": [{"path": f"/srv/{FAKE_KEY}"}]}):
            with self.subTest(change):
                self.assertNotIn(FAKE_KEY, json.dumps(self.refused("secret-refused", self.door.plan, change,
                                                                   base=self.home)))

    def test_the_auto_update_switch_file_is_the_doors(self):
        import maintenance
        plan = self.door.plan({"updates.auto": False}, base=self.home)
        self.assertEqual(plan["needsPerson"], ["updates.auto"])
        self.refused("operator-decision-required", self.door.apply, plan["digest"], actor="agent:mcp",
                     base=self.home)
        done = self.door.apply(plan["digest"], actor="person:cli", base=self.home)
        self.assertEqual((self.home / "auto-update").read_text(encoding="utf-8"), "off\n")
        self.assertIs(maintenance.auto_enabled(self.home), False)
        self.assertIs(self.door.read("updates.auto", base=self.home), False)
        self.door.undo(done["journal"], actor="person:cli", base=self.home)
        self.assertFalse((self.home / "auto-update").exists())
        self.assertIs(maintenance.auto_enabled(self.home), True)
        # An install that turned updates off before 0.19.0 still reads as off while the file is absent.
        settings = self.read("settings.json")
        settings["updates"] = {"auto": False}
        workspace.write_json(self.home / "config" / "settings.json", settings)
        self.assertIs(self.door.read("updates.auto", base=self.home), False)
        self.assertIs(maintenance.auto_enabled(self.home), False)
        # One reading of the word for both switches: only `off` is off, a link is no switch.
        (self.home / "analytics").write_text("  OFF \n", encoding="utf-8")
        import usage_analytics
        self.assertEqual((usage_analytics.read_switch(self.home), self.door.read("analytics.send", base=self.home)),
                         ("off", False))

    def test_the_analytics_switch_keeps_what_off_deletes(self):
        import usage_analytics
        folder = usage_analytics.folder(self.home)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "install-id").write_text("x", encoding="utf-8")
        self.apply({"analytics.send": False}, actor="agent:mcp")         # turning it off sends nothing: safe
        self.assertEqual(usage_analytics.read_switch(self.home), "off")
        self.assertFalse((folder / "install-id").exists(), "off deletes the install id, as the command did")
        plan = self.door.plan({"analytics.send": True}, base=self.home)
        self.assertEqual(plan["needsPerson"], ["analytics.send"])

    def test_retention_keeps_every_key_and_its_reader_reads_through_the_door(self):
        before = self.read("retention.json")
        plan = self.door.plan({"retention.events_days": 30}, base=self.home)
        self.assertEqual(plan["needsPerson"], ["retention.events_days"])      # removes sooner
        self.door.apply(plan["digest"], actor="person:cli", base=self.home)
        after = self.read("retention.json")
        self.assertEqual(after, {**before, "events_days": 30})
        import paths
        importlib.reload(paths)
        from collectors import scan_events
        importlib.reload(scan_events)
        self.assertEqual(scan_events.retention_horizon_days(), 30)
        # Back to the default writes the default: the store's pruner reads every key and never a missing one.
        self.apply({"retention.events_days": None})
        self.assertEqual(self.read("retention.json"), before)

    def test_a_file_made_by_the_door_is_the_readers_whole_document(self):
        import review_policy
        self.assertFalse((self.home / "config" / "review-policy.json").exists())
        self.apply({"review.remind.count": 5, "review.rules.registry.resources-append": False})
        policy = review_policy.load(self.home / "config" / "review-policy.json")
        self.assertNotEqual(policy.state, "invalid", policy.problem)
        self.assertEqual(policy.remind["count"], 5)
        self.assertNotIn("registry.resources-append", policy.rules_on())
        self.apply({"review.rules.registry.resources-append": None})              # back to the default: on
        self.assertIn("registry.resources-append", review_policy.load(self.home / "config" / "review-policy.json")
                      .rules_on())
        self.apply({"relay.enabled": True, "relay.port": 47317})
        self.assertEqual(self.read("relay.json")["schema"], "observatory-relay/1")
        self.apply({"machine_backup.limits.total_mb": 400})
        self.assertEqual(self.read("machine-backup.json"), {"limits": {"total_mb": 400}})

    def test_a_structured_value_is_checked_by_its_owner(self):
        self.refused("invalid-value", self.door.plan, {"tracked.local_folders": ["/srv/alpha-web"]}, base=self.home)
        self.refused("invalid-value", self.door.plan, {"retention.ledger.never": {"not": "a list"}}, base=self.home)
        rows = [{"path": str(self.base / "alpha-web"), "why": "a folder outside Git"}]
        self.apply({"tracked.local_folders": rows})
        import tracked
        self.assertEqual(tracked.load(self.home / "config" / "tracked.json")["local_folders"], rows)

    def test_the_meter_reads_the_limit_the_door_set(self):
        self.refused("operator-decision-required", self.door.apply,
                     self.door.plan({"meter.estateMonthUsd": 2000}, base=self.home)["digest"], actor="agent:mcp",
                     base=self.home)
        self.apply({"meter.estateMonthUsd": 500, "meter.alertRatio": 0.5}, actor="agent:mcp")   # lowering is safe
        self.assertEqual(self.read("meter.json"), {"estateMonthUsd": 500, "alertRatio": 0.5})
        self.assertEqual(self.door.read("meter.estateMonthUsd", base=self.home), 500)

if __name__ == "__main__":
    unittest.main()
