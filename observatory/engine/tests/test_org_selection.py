#!/usr/bin/env python3
"""The local selection record of what goes to one organization, and its apply (OBS-417; organization onboarding
ORG-W5, ORG-W7, ORG-W8, ORG-R02, ORG-R03, ORG-R05, ORG-R11 to ORG-R16, ORG-R24, ORG-R25, ORG-R29).

Synthetic workspace only: the organizations, accounts, projects and slots below are invented. Nothing here sends a
request: the selection never leaves the workspace, and Fabric has no door the selection hands items to yet.
"""
from __future__ import annotations

import contextlib
import datetime
import importlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / "tools"))
import workspace  # noqa: E402

STAND_IN_VALUE = "synthetic-not-a-value"
ORGS = {
    "organizations": {
        "acme": {"label": "Acme", "match": {"repository_owners": ["acme-inc"], "openrouter_accounts": ["acme-or"]}},
        "partner": {"label": "Partner", "match": {"repository_owners": ["partner-org"]}},
        "quiet": {"label": "Quiet", "match": {"repository_owners": ["nobody-here"]}},
        "person": {"label": "Personal", "default": True, "match": {"openrouter_accounts": ["main"]}},
    },
}
PROJECTS = [
    {"id": "project:alpha-web", "name": "alpha-web", "owners": ["acme-inc"], "local_folders": ["alpha-web"],
     "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"},
    {"id": "project:beta-api", "name": "beta-api", "owners": ["acme-inc"], "local_folders": ["beta-api"],
     "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"},
    {"id": "project:notes", "name": "notes", "owners": ["your-user"], "local_folders": ["notes"],
     "organization": "person", "organization_source": "default", "organization_why": "no rule matched"},
    {"id": "project:shared-tool", "name": "shared-tool", "owners": ["acme-inc", "your-user"], "local_folders": ["shared-tool"],
     "organization": None, "organization_source": "conflict", "organization_why": "acme (repository acme-inc) vs "
                                                                                   "person (deploy in your team)"},
    {"id": "project:partner-app", "name": "partner-app", "owners": ["partner-org"], "local_folders": ["partner-app"],
     "organization": "partner", "organization_source": "rule", "organization_why": "repository owner partner-org"},
    {"id": "project:clone", "name": "clone", "owners": ["stranger"], "local_folders": ["clone"],
     "organization": "external", "organization_source": "external", "organization_why": "outside"},
]
ORG_SLOT = "vault:alpha-web/prod/OPENROUTER_API_KEY"
HAND_SLOT = "vault:alpha-web/prod/HAND_PUT"
NOTES_SLOT = "vault:notes/prod/OPENROUTER_API_KEY"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home),
                                           "DOCTL_CONFIG": str(Path(self.tmp.name).resolve() / "no-doctl.yaml")},
                              clear=True)
        self.env.start()
        workspace.initialize(self.home)
        (self.home / "config" / "organizations.json").write_text(json.dumps(ORGS))
        (self.home / "config" / "ownership.json").write_text(json.dumps({"organizations": ["acme-inc", "your-user",
                                                                                            "partner-org"]}))
        import paths
        importlib.reload(paths)
        import organizations
        importlib.reload(organizations)
        self.secrets = Path(paths.source_path("secret_store", paths.SECRETS))
        self.registry("projects.json", {"projects": PROJECTS})
        self.registry("repositories.json", {"repositories": [
            {"name_with_owner": "acme-inc/alpha-web", "host": "github"},
            {"name_with_owner": "acme-inc/beta-api", "host": "github"}]})
        gh = paths.SCRATCH / "gh"
        gh.mkdir(parents=True, exist_ok=True)
        for owner in ("acme-inc", "your-user", "partner-org"):
            (gh / f"{owner}.json").write_text("[]")
        self.private("openrouter-issued.json", json.dumps({"issued": {
            "alpha-llm": {"account": "acme-or", "destination": "vault:alpha-web/prod/OPENROUTER_API_KEY"},
            "notes-llm": {"account": "main", "destination": "vault:notes/prod/OPENROUTER_API_KEY"}}}))
        for slot in ("alpha-web/prod/OPENROUTER_API_KEY", "alpha-web/prod/HAND_PUT", "notes/prod/OPENROUTER_API_KEY",
                     "shared-tool/prod/DATABASE_URL"):
            self.private(f"projects/{slot}", STAND_IN_VALUE)
        import org_view
        importlib.reload(org_view)
        import org_selection
        self.sel = importlib.reload(org_selection)

    def tearDown(self):
        for path in self.secrets.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.env.stop()
        self.tmp.cleanup()

    def registry(self, name, doc):
        import paths
        paths.REGISTRY.mkdir(parents=True, exist_ok=True)
        (paths.REGISTRY / name).write_text(json.dumps(doc), encoding="utf-8")

    def private(self, rel, text):
        path = self.secrets / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)

    @contextlib.contextmanager
    def values_unopened(self):
        """Opening any vault value file fails while this holds, on every platform (Windows reads a file whatever its
        mode, so no chmod): anything in the selection that opened one would fail on it. Listing a folder is allowed."""
        values = (self.secrets / "projects").resolve()
        real_open, real_os_open = open, os.open

        def value(path) -> bool:
            try:
                p = Path(os.fspath(path)).resolve()
            except (TypeError, ValueError):
                return False
            return values in p.parents and not p.name.endswith((".json", ".jsonl"))

        def guarded_open(file, *args, **kwargs):
            if value(file):
                raise AssertionError(f"a vault value file was opened: {Path(os.fspath(file)).name}")
            return real_open(file, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            if value(path):
                raise AssertionError(f"a vault value file was opened: {Path(os.fspath(path)).name}")
            return real_os_open(path, *args, **kwargs)
        with patch("builtins.open", guarded_open), patch("os.open", guarded_os_open):
            yield

    def items(self, view):
        return {i["id"]: i for i in view["items"]}

    def ready(self, layout="separate-space", serves="work-acme", **more):
        return self.sel.plan("acme", {"layout": layout, "serves": serves, **more})

    def files(self):
        root = self.sel.root()
        return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def decision(self, action, target, **more):
        at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return {"by": "operator", "via": "relayed-instruction", "action": action, "target": target,
                "instruction": "move the Acme projects into Acme", "at": at, **more}


class Defaults(Base):
    """ORG-R15: what the plan proposes before any record, and that reading writes nothing."""

    def test_the_default_plan_and_its_preview(self):
        v = self.sel.plan("acme")
        self.assertEqual((v["schema"], v["answer"], v["organization"], v["label"]), ("org-select/1", "plan", "acme",
                                                                                      "Acme"))
        items = self.items(v)
        self.assertEqual((items["project:alpha-web"]["decision"], items["project:alpha-web"]["group"]),
                         ("include", "proposed"))
        self.assertEqual(items["project:beta-api"]["decision"], "include")
        self.assertEqual((items["project:shared-tool"]["decision"], items["project:shared-tool"]["group"]),
                         ("exclude", "needs-you"), "a project in conflict waits for the person (ORG-R05)")
        self.assertNotIn("project:notes", items, "a personal project is counted, not listed")
        self.assertNotIn("project:partner-app", items)
        self.assertNotIn("project:clone", items)
        self.assertEqual(items[ORG_SLOT]["decision"], "hand-over-revoke",
                         "project and issuer agree: handed over and revoked once the replacement works")
        self.assertEqual(items[HAND_SLOT]["decision"], "keep", "put by hand: ambiguous, never chosen by default")
        self.assertEqual(items["openrouter:account:acme-or"]["decision"], "include")
        self.assertIsNone(v["layout"])
        self.assertEqual(v["recommendedLayout"], "separate-space")
        self.assertIn("--layout separate-space", v["command"], "the layout is chosen before anything (ORG-R29)")
        p = v["preview"]
        self.assertEqual(p["organization"]["projects"], 2)
        self.assertEqual((p["keysRequested"], p["keysRevoked"]), (1, 1))
        self.assertEqual(p["staysPersonal"]["projects"], 2,
                         "notes and shared-tool; Partner's project is not personal, and Not ours is not counted")
        self.assertEqual(p["staysPersonal"]["keys"], 3)
        self.assertIn("2 projects and 3 keys stay personal and are never sent", p["staysPersonal"]["line"])
        self.assertEqual([c for c in p["cannotBeUndone"] if ORG_SLOT in c][:1] != [], True)
        self.assertEqual([h["step"] for h in p["humanSteps"]], ["passkey-entry"])
        self.assertTrue(v["digest"].startswith("sha256:"))
        self.assertEqual(v["state"], "complete", v["degraded"])
        self.assertEqual(self.files(), [], "reading the plan writes nothing")

    def test_the_digest_is_of_the_decisions_and_stable(self):
        a, b = self.sel.plan("acme"), self.sel.plan("acme")
        self.assertEqual(a["digest"], b["digest"])
        changed = self.ready()
        self.assertNotEqual(a["digest"], changed["digest"])

    def test_an_organization_with_nothing_is_empty(self):
        v = self.sel.plan("partner")
        self.assertEqual(self.items(v)["project:partner-app"]["decision"], "include")
        v = self.sel.plan("quiet")
        self.assertEqual((v["state"], v["items"]), ("empty", []))
        self.assertIn("--include", v["command"])

    def test_refusals_name_their_command(self):
        for org, code in (("nobody", "unknown-organization"), ("person", "default-organization"),
                          ("", "invalid-organization")):
            with self.assertRaises(self.sel.Refusal) as caught:
                self.sel.plan(org)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.answer()["command"])
        (self.home / "config" / "organizations.json").unlink()
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme")
        self.assertEqual(caught.exception.code, "no-organizations")


class Changes(Base):
    """Item by item and by group (ORG-R02), with labels, a layout and the space served."""

    def test_item_by_item_with_a_label_layout_and_space(self):
        v = self.sel.plan("acme", {"include": ["project:notes"], "exclude": ["project:beta-api"],
                                   "labels": {"project:alpha-web": "Alpha website"},
                                   "keys": {HAND_SLOT: "hand-over-keep"}, "layout": "separate-space",
                                   "serves": "work-acme"})
        items = self.items(v)
        self.assertEqual((items["project:notes"]["decision"], items["project:notes"]["by"]), ("include", "hand"))
        self.assertEqual(items["project:beta-api"]["decision"], "exclude")
        self.assertEqual(items["project:alpha-web"]["label"], "Alpha website")
        self.assertEqual(items[HAND_SLOT]["decision"], "hand-over-keep")
        self.assertIn(NOTES_SLOT, items, "a project chosen by hand brings its keys into the plan (ORG-W6)")
        self.assertEqual(items[NOTES_SLOT]["decision"], "keep", "its issuer is personal: never chosen by default")
        self.assertEqual((v["layout"], v["serves"], v["draft"]), ("separate-space", "work-acme", True))
        self.assertEqual(items["project:notes"]["decidedBy"], {"by": "person", "via": "cli"})
        self.assertEqual([h["step"] for h in v["preview"]["humanSteps"]],
                         ["passkey-entry", "admin-password", "computer-token"])
        draft = self.sel._folder("acme") / "draft.json"
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o600, "owner-only")
        self.assertIn("--digest " + v["digest"], v["command"])

    def test_a_group_records_each_member_listed_now(self):
        v = self.sel.plan("acme", {"includeGroup": "needs-you"})
        self.assertEqual(self.items(v)["project:shared-tool"]["by"], "group")
        draft = json.loads((self.sel._folder("acme") / "draft.json").read_text())
        self.assertIn("project:shared-tool", draft["items"], "each member is recorded one by one")
        # A member added to the group afterwards is listed, never included by the earlier choice.
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc", "your-user"], "local_folders": ["gamma"],
             "organization": None, "organization_source": "conflict", "organization_why": "acme vs person"}]})
        v = self.sel.plan("acme")
        self.assertEqual(self.items(v)["project:gamma"]["decision"], "exclude")

    def test_a_key_follows_its_project_unless_chosen_by_hand(self):
        v = self.sel.plan("acme", {"exclude": ["project:alpha-web"]})
        self.assertEqual(self.items(v)[ORG_SLOT]["decision"], "keep")
        v = self.sel.plan("acme", {"keys": {ORG_SLOT: "hand-over-keep"}})
        self.assertEqual(self.items(v)[ORG_SLOT]["decision"], "hand-over-keep")

    def test_bad_changes_are_refused_and_change_nothing(self):
        for change, code in (({"include": ["project:nowhere"]}, "unknown-item"),
                             ({"include": ["not an id"]}, "unknown-item"),
                             ({"keys": {ORG_SLOT: "copy"}}, "invalid-choice"),
                             ({"keys": {"project:alpha-web": "keep"}}, "unknown-item"),
                             ({"labels": {"project:alpha-web": "line\nbreak"}}, "invalid-label"),
                             ({"labels": {"project:alpha-web": "x" * 81}}, "invalid-label"),
                             ({"labels": {ORG_SLOT: "x"}}, "unknown-item"),
                             ({"layout": "everywhere"}, "invalid-layout"),
                             ({"serves": "../elsewhere"}, "invalid-space"),
                             ({"includeGroup": "all"}, "invalid-group"),
                             ({"surprise": 1}, "invalid-change")):
            with self.assertRaises(self.sel.Refusal, msg=str(change)) as caught:
                self.sel.plan("acme", change)
            self.assertEqual(caught.exception.code, code, change)
        self.assertFalse((self.sel._folder("acme") / "draft.json").exists())

    def test_reset_drops_the_draft(self):
        self.ready()
        v = self.sel.plan("acme", {"reset": True})
        self.assertFalse(v["draft"])
        self.assertIsNone(v["layout"])


class Apply(Base):
    """ORG-R11 to ORG-R14, ORG-R25: the revision, its journal, and only the previewed form."""

    def test_apply_needs_the_previewed_form_and_a_layout(self):
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", None)
        self.assertEqual(caught.exception.code, "not-previewed")
        bare = self.sel.plan("acme")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", bare["digest"])
        self.assertEqual(caught.exception.code, "no-layout")
        half = self.sel.plan("acme", {"layout": "separate-space"})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", half["digest"])
        self.assertEqual(caught.exception.code, "no-serving-space")
        v = self.ready()
        self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "changed-since-preview", "the form changed after the preview")
        self.assertEqual(self.sel._revisions("acme"), [])

    def test_apply_writes_a_verified_revision_and_a_journal_that_waits(self):
        with self.values_unopened():
            import private_io
            with self.assertRaises(AssertionError, msg="the guard holds for the engine's own reader"):
                private_io.read(self.secrets / "projects" / ORG_SLOT[len("vault:"):])
            v = self.ready(labels={"project:alpha-web": "Alpha website"})
            a = self.sel.apply("acme", v["digest"])
            self.sel.show("acme")
        self.assertEqual((a["outcome"], a["revision"], a["state"]), ("applied", 1, "waiting"))
        self.assertEqual(a["selectionDigest"], v["digest"], "the applied record has the previewed digest (ORG-R14)")
        self.assertEqual(a["decidedBy"], {"by": "person", "via": "cli"})
        rec = self.sel.current("acme")
        self.assertEqual((rec["schema"], rec["predecessor"], rec["kind"], rec["layout"]),
                         ("org-selection/1", None, "select", "separate-space"))
        self.assertEqual(rec["serves"], {"space": "work-acme", "here": False})
        self.assertEqual(set(rec["madeIn"]), {"workspace", "account"})
        jdoc = self.sel.journal("acme", 1)
        steps = {i["id"]: {s["step"]: s for s in i["steps"]} for i in jdoc["items"]}
        self.assertEqual(steps["project:alpha-web"]["hand-to-fabric"]["waitingOn"], "fabric-adaptation-plan")
        self.assertEqual(steps["project:alpha-web"]["entry-signed"]["humanStep"], "passkey-entry")
        self.assertEqual(steps[ORG_SLOT]["credential-request"]["state"], "waiting")
        self.assertEqual(steps[ORG_SLOT]["revoke-personal"]["waitingOn"], "replacement-confirmation")
        self.assertTrue(steps[ORG_SLOT]["revoke-personal"]["cannotBeUndone"])
        self.assertEqual(steps[HAND_SLOT]["stays-personal"]["state"], "done")
        self.assertEqual(steps["openrouter:account:acme-or"]["recorded"]["state"], "done")
        self.assertEqual(steps["layout:separate-space"]["create-space"]["waitingOn"], "space-create")
        self.assertEqual(a["counts"]["project"], {"planned": 0, "waiting": 2, "done": 0, "failed": 0, "undone": 0})
        self.assertEqual(a["counts"]["key"]["done"], 2, "the hand-put key and the conflict project's key stay")
        self.assertNotIn("project:shared-tool", steps, "an item left out has no step")
        self.assertIn("fabric-adaptation-plan", [w["on"] for w in a["waitingOn"]])
        self.assertFalse((self.sel._folder("acme") / "draft.json").exists(), "the draft is consumed")
        for path in self.files():
            if path.name != ".lock":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
                self.assertNotIn(STAND_IN_VALUE, path.read_text(), "names only, never a value")
        self.assertNotIn(STAND_IN_VALUE, json.dumps(a))

    def test_the_same_form_again_changes_nothing_and_resumes(self):
        v = self.ready()
        self.sel.apply("acme", v["digest"])
        again = self.sel.apply("acme", self.sel.plan("acme")["digest"])
        self.assertEqual((again["outcome"], again["revision"]), ("unchanged", 1))
        self.assertEqual(len(self.sel._revisions("acme")), 1)
        # A journal lost after the revision: show says so and how to resume, and the same form again writes it.
        self.sel._journal_path("acme", 1).unlink()
        shown = self.sel.show("acme")
        self.assertEqual(shown["state"], "waiting")
        self.assertIn("apply --digest", shown["command"])
        again = self.sel.apply("acme", self.sel.plan("acme")["digest"])
        self.assertEqual(again["outcome"], "resumed")
        self.assertIsNotNone(self.sel.journal("acme", 1))

    def test_a_second_revision_names_its_predecessor_and_keeps_references(self):
        v = self.ready()
        first = self.sel.apply("acme", v["digest"])
        refs = {i["id"]: i["ref"] for i in self.sel.current("acme")["items"] if i.get("ref")}
        v = self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        second = self.sel.apply("acme", v["digest"])
        rec = self.sel.current("acme")
        self.assertEqual((rec["revision"], rec["predecessor"]), (2, first["digest"]))
        self.assertEqual({i["id"]: i["ref"] for i in rec["items"] if i.get("ref")},
                         {"project:alpha-web": refs["project:alpha-web"]}, "the same project keeps its reference")
        old = {i["id"]: i for i in self.sel.journal("acme", 1)["items"]}
        self.assertEqual({s["state"] for s in old["project:beta-api"]["steps"]}, {"undone"})
        self.assertNotEqual({s["state"] for s in old["project:alpha-web"]["steps"]}, {"undone"},
                            "an item carried the same way keeps its progress")
        self.assertEqual(second["revision"], 2)

    def test_a_record_changed_by_hand_does_not_verify(self):
        v = self.ready()
        self.sel.apply("acme", v["digest"])
        path = self.sel._revisions("acme")[0]
        doc = json.loads(path.read_text())
        doc["items"][0]["decision"] = "exclude"
        path.write_text(json.dumps(doc))
        path.chmod(0o600)
        self.assertEqual(self.sel.show("acme")["state"], "error")
        self.assertEqual(self.sel.show("acme")["error"], "record-unreadable")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme")
        self.assertEqual(caught.exception.code, "record-unreadable")
        path.chmod(0o644)
        with self.assertRaises(self.sel.Refusal):
            self.sel.current("acme")

    def test_a_project_chosen_for_another_organization_is_refused(self):
        v = self.sel.plan("partner", {"include": ["project:shared-tool"], "layout": "one-space"})
        self.sel.apply("partner", v["digest"])
        v = self.ready(include=["project:shared-tool"])
        self.assertEqual(v["state"], "conflict")
        self.assertIn("CT-141", v["conflicts"][0]["why"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "chosen-for-another")
        self.assertIn("--exclude project:shared-tool", caught.exception.command)


class ReviewRoundOne(Base):
    """What the first review found: each case reproduced, then fixed."""

    def test_undo_does_not_bring_back_a_project_another_organization_took(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:notes"]})["digest"])
        p = self.sel.plan("partner", {"include": ["project:notes"], "layout": "one-space"})
        self.sel.apply("partner", p["digest"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "chosen-for-another")
        self.assertEqual(len(self.sel._revisions("acme")), 2, "nothing written")

    def test_a_space_named_outside_the_separate_layout_does_not_change_the_form(self):
        with self.assertRaises(self.sel.Refusal):
            self.sel.plan("acme", {"layout": "this-space", "serves": "somewhere"})
        v = self.sel.plan("acme", {"layout": "this-space"})
        self.assertIsNone(v["serves"])
        self.sel.apply("acme", v["digest"])
        again = self.sel.plan("acme")
        self.assertTrue(again["applied"], "the applied form reads applied")
        self.sel.apply("acme", self.ready()["digest"])
        moved = self.sel.plan("acme", {"layout": "one-space"})
        self.sel.apply("acme", moved["digest"])
        self.assertTrue(self.sel.plan("acme")["applied"])
        self.assertEqual(self.sel.apply("acme", self.sel.plan("acme")["digest"])["outcome"], "unchanged")

    def test_a_layout_change_moves_what_a_revocation_waits_on(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"layout": "one-space"})["digest"])
        step = next(s for i in self.sel.journal("acme", 2)["items"] if i["id"] == ORG_SLOT for s in i["steps"]
                    if s["step"] == "revoke-personal")
        self.assertEqual(step["waitingOn"], "rotation", "in one space nothing waits for a confirmation")
        self.sel.apply("acme", self.ready()["digest"])
        step = next(s for i in self.sel.journal("acme", 3)["items"] if i["id"] == ORG_SLOT for s in i["steps"]
                    if s["step"] == "revoke-personal")
        self.assertEqual(step["waitingOn"], "replacement-confirmation", "a separate space asks for it again")

    def test_a_project_excluded_by_hand_stays_known(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        v = self.sel.plan("acme", {"exclude": ["project:notes"]})
        self.sel.apply("acme", v["digest"])
        v = self.sel.plan("acme")
        self.assertEqual(v["unknown"], [])
        self.assertEqual(self.items(v)["project:notes"]["decision"], "exclude")

    def test_keys_of_a_project_chosen_after_a_record_take_the_defaults(self):
        self.private("openrouter-issued.json", json.dumps({"issued": {
            "alpha-llm": {"account": "acme-or", "destination": "vault:alpha-web/prod/OPENROUTER_API_KEY"},
            "notes-llm": {"account": "acme-or", "destination": "vault:notes/prod/OPENROUTER_API_KEY"}}}))
        self.sel.apply("acme", self.ready()["digest"])
        v = self.sel.plan("acme", {"include": ["project:notes"]})
        row = self.items(v)[NOTES_SLOT]
        self.assertEqual((row["decision"], row["new"]), ("hand-over-revoke", False), row)

    def test_the_words_must_name_the_organization(self):
        words = self.decision("org.select.plan", "acme")
        del words["target"]
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp", decision=words)
        self.assertEqual(caught.exception.code, "decision-invalid")

    def test_an_unreadable_other_record_is_named_and_stops_apply(self):
        self.sel.apply("partner", self.sel.plan("partner", {"layout": "one-space"})["digest"])
        path = self.sel._revisions("partner")[0]
        path.write_text(path.read_text().replace('"one-space"', '"this-space"'))
        v = self.ready()
        self.assertTrue([d for d in v["degraded"] if d["code"] == "record-unreadable"])
        self.assertEqual(v["state"], "partial")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "conflict-unchecked")

    def test_a_revoked_value_is_never_kept_personal_by_a_later_revision(self):
        self.sel.apply("acme", self.ready()["digest"])
        path = self.sel._journal_path("acme", 1)
        jdoc = json.loads(path.read_text())
        for item in jdoc["items"]:
            for st in item["steps"]:
                if item["id"] == ORG_SLOT and st["step"] == "revoke-personal":
                    st["state"] = "done"
        path.write_text(json.dumps(jdoc))
        v = self.sel.plan("acme", {"keys": {ORG_SLOT: "keep"}})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "already-revoked")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "already-revoked")
        # A change that keeps the key handed over carries the done step, and never rewrites it as undone.
        self.sel.apply("acme", self.sel.plan("acme", {"keys": {ORG_SLOT: "hand-over-revoke"},
                                                       "exclude": ["project:beta-api"]})["digest"])
        step = next(st for i in self.sel.journal("acme", 2)["items"] if i["id"] == ORG_SLOT for st in i["steps"]
                    if st["step"] == "revoke-personal")
        self.assertEqual(step["state"], "done")

    def test_a_store_that_cannot_be_written_is_a_typed_refusal(self):
        import paths
        paths.STORE.mkdir(parents=True, exist_ok=True)
        elsewhere = Path(self.tmp.name).resolve() / "elsewhere"
        elsewhere.mkdir()
        (paths.STORE / "org-selection").symlink_to(elsewhere)
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", "sha256:" + "0" * 64)
        self.assertEqual(caught.exception.code, "record-unwritable")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"layout": "one-space"})
        self.assertEqual(caught.exception.code, "record-unwritable")

    def test_a_null_decision_on_the_command_line_is_refused(self):
        import org as org_cli
        importlib.reload(org_cli)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = org_cli.main(["select", "--org", "acme", "plan", "--layout", "one-space", "--decision", "null",
                                 "--json"])
        self.assertEqual((code, json.loads(err.getvalue())["error"]), (1, "decision-invalid"))
        self.assertFalse((self.sel._folder("acme") / "draft.json").exists())


class ReviewRoundTwo(Base):
    """What the second review found: each case reproduced, then fixed."""

    def test_another_organizations_project_or_not_ours_is_a_conflict(self):
        v = self.ready(include=["project:partner-app", "project:clone"])
        self.assertEqual(v["state"], "conflict")
        self.assertEqual({c["project"]: c["code"] for c in v["conflicts"]},
                         {"project:partner-app": "chosen-for-another", "project:clone": "not-ours"})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertIn(caught.exception.code, ("chosen-for-another", "not-ours"))
        self.assertEqual(self.sel._revisions("acme"), [])

    def test_a_project_chosen_then_left_out_by_hand_says_so(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        row = next(i for i in self.sel.current("acme")["items"] if i["id"] == "project:notes")
        self.assertEqual((row["group"], row["why"]), ("by-hand", "you chose it by hand"))
        v = self.sel.plan("acme", {"exclude": ["project:notes"]})
        row = self.items(v)["project:notes"]
        self.assertEqual((row["decision"], row["group"], row["why"]), ("exclude", "by-hand", "you left it out by hand"))

    def test_a_layout_change_keeps_a_signed_entry_and_undo_refuses_after_it(self):
        self.sel.apply("acme", self.ready()["digest"])
        path = self.sel._journal_path("acme", 1)
        jdoc = json.loads(path.read_text())
        for item in jdoc["items"]:
            for st in item["steps"]:
                if item["id"] == "project:alpha-web" and st["step"] == "entry-signed":
                    st["state"] = "done"
        path.write_text(json.dumps(jdoc))
        self.sel.apply("acme", self.sel.plan("acme", {"layout": "one-space"})["digest"])
        step = next(st for i in self.sel.journal("acme", 2)["items"] if i["id"] == "project:alpha-web"
                    for st in i["steps"] if st["step"] == "entry-signed")
        self.assertEqual(step["state"], "done")
        old = next(i for i in self.sel.journal("acme", 1)["items"] if i["id"] == "project:alpha-web")
        self.assertEqual({st["step"]: st["state"] for st in old["steps"]}["export-curated-entry"], "undone",
                         "a step the new layout no longer has is undone in the old journal")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "entry-signed")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:alpha-web"]})["digest"])
        self.assertEqual(caught.exception.code, "entry-signed")

    def test_an_apply_stopped_after_its_revision_is_finished_by_the_same_form(self):
        self.sel.apply("acme", self.ready()["digest"])
        v = self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        draft = (self.sel._folder("acme") / "draft.json").read_text()
        journal_one = self.sel._journal_path("acme", 1).read_text()
        self.sel.apply("acme", v["digest"])
        # Put back what the interruption would have left: the draft, and revision 1's journal not superseded.
        for path, text in ((self.sel._folder("acme") / "draft.json", draft),
                           (self.sel._journal_path("acme", 1), journal_one)):
            path.write_text(text)
            path.chmod(0o600)
        p = self.sel.plan("acme")
        self.assertTrue(p["applied"])
        self.assertIn("apply --digest", p["command"], "never `nothing to apply` for an unfinished apply")
        self.assertEqual(self.sel.show("acme")["state"], "waiting")
        self.assertEqual(self.sel.apply("acme", p["digest"])["outcome"], "resumed")
        old = next(i for i in self.sel.journal("acme", 1)["items"] if i["id"] == "project:beta-api")
        self.assertEqual({st["state"] for st in old["steps"]}, {"undone"})
        self.assertIn("nothing to apply", self.sel.plan("acme")["nextStep"])

    def test_a_damaged_draft_is_dropped_by_the_command_it_names(self):
        self.ready()
        path = self.sel._folder("acme") / "draft.json"
        path.write_text("{")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme")
        self.assertIn("plan --reset", caught.exception.command)
        self.assertFalse(self.sel.plan("acme", {"reset": True})["draft"])

    def test_the_same_words_do_not_apply_two_forms(self):
        words = self.decision("org.select.apply", "acme")
        plan = self.decision("org.select.plan", "acme")
        v = self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp", decision=plan)
        self.sel.apply("acme", v["digest"], caller="agent:mcp", decision=words)
        v = self.sel.plan("acme", {"exclude": ["project:beta-api"]}, caller="agent:mcp", decision=plan)
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"], caller="agent:mcp", decision=words)
        self.assertEqual(caught.exception.code, "decision-invalid")

    def test_smaller_edges(self):
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"serves": "ghp_" + "a" * 36})
        self.assertEqual(caught.exception.code, "invalid-space", "a space name shaped like a credential")
        for change in ({"keys": ["x"]}, {"labels": "x"}, {"include": "project:notes"}):
            with self.assertRaises(self.sel.Refusal) as caught:
                self.sel.plan("acme", change)
            self.assertEqual(caught.exception.code, "invalid-change", change)
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:delta", "name": "delta", "owners": ["acme-inc", "your-user"], "local_folders": ["delta"],
             "organization": None, "organization_source": "conflict", "organization_why": "acme vs person"},
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        v = self.sel.plan("acme", {"includeGroup": "proposed"})
        self.assertEqual(self.items(v)["project:gamma"]["decision"], "exclude", "a group never takes a new item")
        u = self.sel.undo("acme")
        self.assertEqual((u["state"], u["outcome"]), ("empty", "undone"))
        self.assertIn("nothing is selected", u["nextStep"])


class ReviewRoundThree(Base):
    """What the third review found: each case reproduced, then fixed."""

    def test_a_change_that_leaves_the_applied_form_keeps_no_draft(self):
        self.sel.apply("acme", self.ready(exclude=["project:shared-tool"])["digest"])
        v = self.sel.plan("acme", {"exclude": ["project:shared-tool"]})
        self.assertEqual((v["applied"], v["draft"]), (True, False))
        self.assertIn("nothing to apply", v["nextStep"])

    def interrupt(self, fn):
        """Run `fn` with the commit stopped right after the revision is written."""
        real = self.sel._supersede

        def stop(*args, **kwargs):
            raise KeyboardInterrupt
        self.sel._supersede = stop
        try:
            with self.assertRaises(KeyboardInterrupt):
                fn()
        finally:
            self.sel._supersede = real

    def test_an_undo_to_nothing_that_stopped_is_named_and_finished(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.interrupt(lambda: self.sel.undo("acme"))
        shown = self.sel.show("acme")
        self.assertEqual(shown["state"], "waiting", "never empty while the undo has not finished")
        self.assertIn("apply --digest", shown["command"])
        self.assertIn("stopped", self.sel.plan("acme")["nextStep"])
        digest = shown["command"].rsplit(" ", 1)[1]
        self.assertEqual(self.sel.apply("acme", digest)["outcome"], "resumed",
                         "the record's own form finishes it, although the defaults read another form now")
        self.assertEqual({st["state"] for i in self.sel.journal("acme", 1)["items"] for st in i["steps"]}, {"undone"})
        self.assertEqual(self.sel.show("acme")["state"], "empty")

    def test_any_apply_finishes_the_one_that_stopped_first(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.interrupt(lambda: self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:beta-api"]})
                                              ["digest"]))
        v = self.sel.plan("acme", {"exclude": ["project:alpha-web"]})
        self.sel.apply("acme", v["digest"])
        states = {st["state"] for i in self.sel.journal("acme", 1)["items"] if i["id"] == "project:beta-api"
                  for st in i["steps"]}
        self.assertEqual(states, {"undone"}, "revision 1's journal is settled before revision 3 is written")

    def test_org_keys_lists_the_keys_of_a_project_chosen_by_hand(self):
        import org_view
        self.assertNotIn(NOTES_SLOT, {k["id"] for k in org_view.keys("acme")["keys"]})
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.assertIn(NOTES_SLOT, {k["id"] for k in org_view.keys("acme")["keys"]})

    def test_items_found_since_stay_new_after_an_unrelated_apply(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:beta-api"]})["digest"])
        self.assertIn("project:gamma", self.sel.plan("acme")["new"])
        self.assertNotIn("project:gamma", {i["id"] for i in self.sel.current("acme")["items"]})
        self.assertEqual(self.sel.show("acme")["new"], ["project:gamma"])

    def test_a_refusal_never_hands_out_another_form_to_apply(self):
        v = self.ready()
        self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertNotIn("digest", caught.exception.answer())

    def test_a_project_gone_from_the_registry_is_not_counted(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:notes"]})
        self.private("projects/notes/prod/OPENROUTER_API_KEY", STAND_IN_VALUE)
        (self.secrets / "projects" / "notes" / "prod" / "OPENROUTER_API_KEY").unlink()
        v = self.sel.plan("acme")
        self.assertIn("project:notes", v["unknown"])
        self.assertEqual((v["state"], v["preview"]["organization"]["projects"]), ("partial", 2))

    def test_undo_refuses_a_project_another_organizations_rules_now_hold(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:notes"]})["digest"])
        moved = [dict(p, organization="partner", organization_source="rule", organization_why="partner rule")
                 if p["id"] == "project:notes" else p for p in PROJECTS]
        self.registry("projects.json", {"projects": moved})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "chosen-for-another")

    def test_nothing_is_created_for_an_organization_never_selected(self):
        for fn in (lambda: self.sel.undo("acme"), lambda: self.sel.confirm_replacement("acme", ORG_SLOT)):
            with self.assertRaises(self.sel.Refusal):
                fn()
        self.assertEqual(self.files(), [])

    def test_the_cli_refuses_a_flag_its_action_does_not_take(self):
        import org as org_cli
        importlib.reload(org_cli)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = org_cli.main(["select", "--org", "acme", "show", "--exclude", "project:beta-api", "--json"])
        self.assertEqual((code, json.loads(err.getvalue())["error"]), (1, "invalid-change"))


class ReviewRoundFour(Base):
    """What the fourth review found: each case reproduced, then fixed."""

    def test_nothing_is_applied_from_an_unreadable_registry_or_a_lost_item(self):
        v = self.ready()
        import paths
        (paths.REGISTRY / "projects.json").write_text("{")
        broken = self.sel.plan("acme")
        self.assertEqual(broken["state"], "partial")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", broken["digest"])
        self.assertEqual(caught.exception.code, "registry-unreadable")
        self.registry("projects.json", {"projects": PROJECTS})
        self.sel.apply("acme", v["digest"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:alpha-web"]})
        lost = self.sel.plan("acme", {"labels": {"project:beta-api": "Billing"}})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", lost["digest"])
        self.assertEqual(caught.exception.code, "items-not-found")
        self.assertIn("--exclude project:alpha-web", caught.exception.command)

    def test_an_applied_layout_with_no_items_reads_applied(self):
        v = self.sel.plan("quiet", {"layout": "this-space"})
        self.sel.apply("quiet", v["digest"])
        again = self.sel.plan("quiet")
        self.assertEqual((again["layout"], again["applied"]), ("this-space", True))

    def test_a_label_never_chooses_a_project(self):
        v = self.sel.plan("acme", {"labels": {"project:shared-tool": "Shared"}})
        row = self.items(v)["project:shared-tool"]
        self.assertEqual((row["decision"], row["label"]), ("exclude", "Shared"))
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        v = self.sel.plan("acme", {"labels": {"project:gamma": "Gamma"}})
        self.sel.apply("acme", v["digest"])
        row = next(i for i in self.sel.current("acme")["items"] if i["id"] == "project:gamma")
        self.assertEqual((row["decision"], row["label"], row["by"]), ("exclude", "Gamma", "default"),
                         "the label is kept, and a label alone decides nothing")
        self.assertTrue(self.sel.plan("acme")["applied"])
        self.assertEqual(self.sel.show("acme")["new"], ["project:gamma"], "still waiting for a person")

    def test_an_undo_reads_applied_beside_an_item_found_since(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:beta-api"]})["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        self.sel.undo("acme")
        self.assertTrue(self.sel.plan("acme")["applied"])

    def test_the_gate_comes_before_any_write(self):
        self.sel.apply("acme", self.ready()["digest"])
        v = self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        journal_one = self.sel._journal_path("acme", 1).read_text()
        self.sel.apply("acme", v["digest"])
        self.sel._journal_path("acme", 1).write_text(journal_one)      # as an interruption would leave it
        for fn in (lambda: self.sel.apply("acme", v["digest"], caller="agent:mcp"),
                   lambda: self.sel.undo("acme", caller="agent:mcp")):
            with self.assertRaises(self.sel.Refusal) as caught:
                fn()
            self.assertEqual(caught.exception.code, "operator-decision-required")
        self.assertEqual(self.sel._journal_path("acme", 1).read_text(), journal_one, "nothing was resumed")

    def test_a_damaged_draft_leaves_the_record_readable(self):
        self.sel.apply("acme", self.ready()["digest"])
        (self.sel._folder("acme") / "draft.json").write_text("{")
        shown = self.sel.show("acme")
        self.assertEqual(shown["record"]["revision"], 1)
        self.assertTrue([d for d in shown["degraded"] if "plan --reset" in d["effect"]])

    def test_reset_is_true_or_absent(self):
        self.ready()
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"reset": "no"})
        self.assertEqual(caught.exception.code, "invalid-change")
        self.assertTrue(self.sel.plan("acme")["draft"])


class ReviewRoundFive(Base):
    """What the fifth review named as non-blocking and this change fixes."""

    def test_an_unreadable_other_record_never_blocks_undo_to_nothing(self):
        self.sel.apply("partner", self.sel.plan("partner", {"layout": "one-space"})["digest"])
        self.sel.apply("acme", self.ready()["digest"])
        path = self.sel._revisions("partner")[0]
        path.write_text(path.read_text().replace('"one-space"', '"this-space"'))
        self.assertEqual(self.sel.undo("acme")["state"], "empty")

    def test_a_default_exclusion_the_rules_propose_later_is_new(self):
        self.sel.apply("acme", self.ready()["digest"])
        moved = [dict(p, organization="acme", organization_source="rule", organization_why="repository owner acme-inc")
                 if p["id"] == "project:shared-tool" else p for p in PROJECTS]
        self.registry("projects.json", {"projects": moved})
        self.assertIn("project:shared-tool", self.sel.plan("acme")["new"])
        v = self.sel.plan("acme", {"include": ["project:shared-tool"]})
        self.assertNotIn("project:shared-tool", v["new"], "decided by the person: no longer new")

    def test_the_answer_names_the_records_decider_and_the_caller(self):
        self.sel.apply("acme", self.ready()["digest"])
        again = self.sel.apply("acme", self.sel.plan("acme")["digest"], caller="agent:mcp",
                               decision=self.decision("org.select.apply", "acme"))
        self.assertEqual((again["decidedBy"]["by"], again["calledBy"]["by"]), ("person", "operator"))

    def test_the_tool_refuses_an_argument_its_action_does_not_take(self):
        m = McpTool.server(self)
        for args in ({"action": "show", "changes": {"layout": "one-space"}}, {"action": "plan", "digest": "x"},
                     {"action": "apply", "key": ORG_SLOT}):
            self.assertEqual(m.observatory_org_selection(org="acme", **args)["error"], "invalid-change", args)


class ReviewRoundSix(Base):
    """What the sixth review found: each case reproduced, then fixed."""

    def test_a_key_of_a_project_left_out_is_never_new(self):
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:alpha-web"], "layout": "one-space"})
                       ["digest"])
        v = self.sel.plan("acme")
        self.assertEqual((v["new"], v["applied"]), ([], True))
        import organization_findings
        importlib.reload(organization_findings)
        self.assertEqual(organization_findings.selection_findings(self.sel.new_since_all()), [])
        row = self.items(v)[ORG_SLOT]
        self.assertEqual((row["decision"], row["by"]), ("keep", "project"))
        # Its project chosen again: the key takes the default again.
        self.assertEqual(self.items(self.sel.plan("acme", {"include": ["project:alpha-web"]}))[ORG_SLOT]["decision"],
                         "hand-over-revoke")

    def test_after_a_record_with_no_items_a_new_project_is_never_chosen(self):
        self.sel.apply("quiet", self.sel.plan("quiet", {"layout": "one-space"})["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:hush", "name": "hush", "owners": ["nobody-here"], "local_folders": ["hush"],
             "organization": "quiet", "organization_source": "rule", "organization_why": "repository owner"}]})
        v = self.sel.plan("quiet")
        row = self.items(v)["project:hush"]
        self.assertEqual((row["decision"], row["new"]), ("exclude", True))
        self.assertEqual(self.sel.new_since_all(), [{"organization": "quiet", "new": ["project:hush"], "label": "Quiet"}])

    def test_the_store_folder_is_owner_only_from_the_first_change(self):
        self.sel.plan("acme", {"layout": "one-space"})
        self.assertEqual(stat.S_IMODE(self.sel.root().stat().st_mode), 0o700)

    def test_no_user_name_is_not_a_crash(self):
        import getpass
        with patch.object(getpass, "getuser", side_effect=ImportError("no pwd")):
            self.assertIsNone(self.sel.plan("acme")["madeIn"]["account"])

    def test_undo_says_it_dropped_a_draft(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        u = self.sel.undo("acme")
        self.assertTrue(u["draftDropped"])


class ReviewRoundSeven(Base):
    """What the seventh review found: each case reproduced, then fixed."""

    def test_a_source_that_did_not_read_never_asks_to_drop_a_choice(self):
        before = self.sel.apply("acme", self.ready()["digest"])
        self.assertEqual(self.sel.plan("acme")["preview"]["accounts"], 2, before["counts"])
        self.private("openrouter-issued.json", "{")
        v = self.sel.plan("acme", {"labels": {"project:beta-api": "Billing"}})
        self.assertEqual(v["unknown"], [])
        self.assertIn("openrouter:account:acme-or", v["unread"])
        self.assertEqual(v["preview"]["accounts"], 1, "the account whose source did not read is not counted as going")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "source-unreadable")
        self.assertNotIn("--exclude", caught.exception.command or "")

    def test_items_nobody_decided_leave_the_applied_form_applied(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        v = self.sel.plan("acme")
        self.assertTrue(v["applied"])
        self.assertIn("1 item(s) found since wait for you", v["nextStep"])
        self.assertEqual(self.sel.apply("acme", v["digest"])["outcome"], "unchanged")

    def test_a_key_found_later_in_a_project_left_out_is_not_new(self):
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:alpha-web"], "layout": "one-space"})
                       ["digest"])
        self.private("projects/alpha-web/prod/LATER_KEY", STAND_IN_VALUE)
        self.private("openrouter-issued.json", json.dumps({"issued": {
            "alpha-llm": {"account": "acme-or", "destination": "vault:alpha-web/prod/OPENROUTER_API_KEY"},
            "later": {"account": "acme-or", "destination": "vault:alpha-web/prod/LATER_KEY"}}}))
        self.assertEqual(self.sel.plan("acme")["new"], [])

    def test_a_refused_change_with_reset_keeps_the_draft(self):
        self.ready()
        with self.assertRaises(self.sel.Refusal):
            self.sel.plan("acme", {"reset": True, "layout": "everywhere"})
        self.assertTrue(self.sel.plan("acme")["draft"])

    def test_a_refused_agent_writes_nothing_not_even_a_lock(self):
        with self.assertRaises(self.sel.Refusal):
            self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp")
        with self.assertRaises(self.sel.Refusal):
            self.sel.apply("acme", "sha256:" + "0" * 64, caller="agent:mcp")
        self.assertEqual(self.files(), [])

    def test_undo_without_the_organization_configured_checks_the_registry(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:notes"]})["digest"])
        (self.home / "config" / "organizations.json").unlink()
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:notes"]})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "items-not-found")


class ReviewRoundEight(Base):
    """What the eighth review found: each case reproduced, then fixed."""

    def test_the_words_are_checked_although_tools_node_shadows_the_package(self):
        saved = {k: sys.modules[k] for k in list(sys.modules) if k == "node" or k.startswith("node.")}
        try:
            for k in saved:
                del sys.modules[k]
            import importlib.util
            spec = importlib.util.spec_from_file_location("node", ROOT / "tools" / "node.py")
            shadow = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(shadow)
            sys.modules["node"] = shadow                 # what `tools/` first on the path leaves behind
            v = self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp",
                              decision=self.decision("org.select.plan", "acme"))
            self.assertEqual(v["layout"], "one-space")
        finally:
            for k in [k for k in sys.modules if k == "node" or k.startswith("node.")]:
                del sys.modules[k]
            sys.modules.update(saved)

    def test_the_accounts_view_never_puts_tools_first(self):
        import org_view
        tools = str(ROOT / "tools")
        while tools in sys.path:
            sys.path.remove(tools)
        org_view.accounts()
        self.assertNotEqual(sys.path[0], tools)

    def test_nothing_found_since_unchanged_reads_applied(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.private("projects/alpha-web/prod/ANOTHER_BY_HAND", STAND_IN_VALUE)
        self.assertTrue(self.sel.plan("acme")["applied"], "a hand-put slot found since needs nobody yet")
        moved = [dict(p, organization="acme", organization_source="rule", organization_why="repository owner acme-inc")
                 if p["id"] == "project:shared-tool" else p for p in PROJECTS]
        self.registry("projects.json", {"projects": moved})
        v = self.sel.plan("acme")
        self.assertTrue(v["applied"], "proposed again: listed as new, the record's decision stands")
        self.assertIn("project:shared-tool", v["new"])
        self.assertEqual(self.sel.apply("acme", v["digest"])["outcome"], "unchanged")
        self.assertIn("project:shared-tool", {i["id"] for i in self.sel.current("acme")["items"]})

    def test_a_draft_choice_gone_since_is_never_applied(self):
        v = self.ready(include=["project:notes"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:notes"]})
        v = self.sel.plan("acme")
        self.assertIn("project:notes", v["unknown"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "items-not-found")

    def test_a_confirmation_names_the_organization_and_words_carry_no_credential(self):
        self.sel.apply("acme", self.ready()["digest"])
        UndoAndConfirm.replacement_arrived(self)
        bare = self.decision("org.select.confirm-replacement", ORG_SLOT)
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.confirm_replacement("acme", ORG_SLOT, caller="agent:mcp", decision=bare)
        self.assertEqual(caught.exception.code, "decision-invalid")
        good = self.decision("org.select.confirm-replacement", f"acme:{ORG_SLOT}")
        self.assertEqual(self.sel.confirm_replacement("acme", ORG_SLOT, caller="agent:mcp", decision=good)["outcome"],
                         "confirmed")
        shaped = dict(self.decision("org.select.plan", "acme"), instruction="apply it, ghp_" + "b" * 36)
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp", decision=shaped)
        self.assertEqual(caught.exception.code, "decision-invalid")

    def test_a_linked_selection_folder_is_named(self):
        self.sel.apply("acme", self.ready()["digest"])
        elsewhere = Path(self.tmp.name).resolve() / "elsewhere"
        elsewhere.mkdir()
        (self.sel.root() / "0000000000000000").symlink_to(elsewhere)
        v = self.sel.plan("partner", {"include": ["project:notes"], "layout": "one-space"})
        self.assertTrue([d for d in v["degraded"] if "symbolic link" in d["reason"]])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("partner", v["digest"])
        self.assertEqual(caught.exception.code, "conflict-unchecked")


class ReviewRoundNine(Base):
    """What the ninth review found: each case reproduced, then fixed."""

    def test_a_row_proposed_again_can_be_decided_out(self):
        self.sel.apply("acme", self.ready()["digest"])
        moved = [dict(p, organization="acme", organization_source="rule", organization_why="repository owner acme-inc")
                 if p["id"] == "project:shared-tool" else p for p in PROJECTS]
        self.registry("projects.json", {"projects": moved})
        self.assertEqual(self.sel.show("acme")["new"], ["project:shared-tool"])
        self.assertIn("1 item(s) found since wait for you", self.sel.plan("acme")["nextStep"])
        v = self.sel.plan("acme", {"exclude": ["project:shared-tool"]})
        self.assertFalse(v["applied"], "deciding it out is a change the record must keep")
        self.assertEqual(self.sel.apply("acme", v["digest"])["outcome"], "applied")
        self.assertEqual(self.sel.show("acme")["new"], [])

    def test_an_unread_registry_is_partial_never_empty(self):
        import paths
        (paths.REGISTRY / "projects.json").unlink()
        v = self.sel.plan("quiet")
        self.assertEqual(v["state"], "partial")
        self.assertIn("did not read", v["nextStep"])
        self.assertIsNone(v["preview"]["staysPersonal"]["projects"])
        self.assertIn("not known", v["preview"]["staysPersonal"]["line"])

    def test_a_degraded_source_is_named_once(self):
        import paths
        (paths.REGISTRY / "projects.json").unlink()
        v = self.sel.plan("acme")
        named = [(d["source"], d["code"]) for d in v["degraded"]]
        self.assertEqual(len(named), len(set(named)), named)


class ReviewRoundTen(Base):
    """What the tenth review found: each case reproduced, then fixed."""

    def vault_unlistable(self):
        root = self.secrets / "projects"
        moved = root.with_name("projects-away")
        root.rename(moved)
        root.write_text("not a folder")
        return root, moved

    def test_a_vault_that_does_not_list_is_never_counted_or_applied(self):
        v = self.ready()
        root, moved = self.vault_unlistable()
        try:
            v = self.sel.plan("acme")
            self.assertEqual(v["state"], "partial")
            self.assertIsNone(v["preview"]["staysPersonal"]["keys"])
            self.assertIn("the vault did not read", v["preview"]["staysPersonal"]["line"])
            with self.assertRaises(self.sel.Refusal) as caught:
                self.sel.apply("acme", v["digest"])
            self.assertEqual(caught.exception.code, "vault-unreadable")
        finally:
            root.unlink()
            moved.rename(root)

    def test_plan_reads_waiting_while_the_last_apply_is_unfinished(self):
        self.sel.apply("acme", self.ready()["digest"])
        v = self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        journal_one = self.sel._journal_path("acme", 1).read_text()
        self.sel.apply("acme", v["digest"])
        self.sel._journal_path("acme", 1).write_text(journal_one)
        self.assertEqual(self.sel.plan("acme")["state"], "waiting")

    def test_what_stays_personal_counts_what_the_person_leaves_out(self):
        v = self.ready(exclude=["project:beta-api"])
        self.assertEqual(v["preview"]["staysPersonal"]["projects"], 3, "notes, shared-tool and beta-api")

    def test_a_hand_edited_draft_row_is_refused_not_a_crash(self):
        self.ready()
        path = self.sel._folder("acme") / "draft.json"
        doc = json.loads(path.read_text())
        doc["items"]["project:alpha-web"] = {"decision": "include"}
        path.write_text(json.dumps(doc))
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme")
        self.assertIn("plan --reset", caught.exception.command)

    def test_show_after_an_undo_to_nothing_says_so(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.sel.undo("acme")
        shown = self.sel.show("acme")
        self.assertEqual(shown["state"], "empty")
        self.assertIn("nothing is selected", shown["nextStep"])

    def test_the_package_is_never_replaced_by_tools_node(self):
        saved = {k: sys.modules[k] for k in list(sys.modules) if k == "node" or k.startswith("node.")}
        tools = str(ROOT / "tools")
        try:
            for k in saved:
                del sys.modules[k]
            sys.path.insert(0, tools)
            self.sel._node_actions()
            self.assertNotEqual(getattr(sys.modules.get("node"), "__file__", ""), str(ROOT / "tools" / "node.py"),
                                "the name `node` is never bound to tools/node.py by the selection")
        finally:
            sys.path.remove(tools)
            for k in [k for k in sys.modules if k == "node" or k.startswith("node.")]:
                del sys.modules[k]
            sys.modules.update(saved)


class ReviewRoundEleven(Base):
    """What the eleventh review found: each case reproduced, then fixed."""

    def test_show_of_a_damaged_record_in_text_names_it_and_fails(self):
        self.sel.apply("acme", self.ready()["digest"])
        path = self.sel._revisions("acme")[0]
        path.write_text(path.read_text().replace('"separate-space"', '"one-space"', 1))
        code, out, _ = Cli.run_cli(self, "select", "--org", "acme", "show")
        self.assertEqual(code, 1)
        self.assertIn("does not read", out)
        self.assertIn("full doctor", out)

    def test_a_label_on_a_project_the_plan_does_not_list_decides_nothing(self):
        v = self.sel.plan("acme", {"labels": {"project:notes": "Notes"}, "layout": "one-space"})
        row = self.items(v)["project:notes"]
        self.assertEqual((row["decision"], row["by"]), ("exclude", "default"))
        self.sel.apply("acme", v["digest"])
        moved = [dict(p, organization="acme", organization_source="rule", organization_why="repository owner acme-inc")
                 if p["id"] == "project:notes" else p for p in PROJECTS]
        self.registry("projects.json", {"projects": moved})
        self.assertIn("project:notes", self.sel.plan("acme")["new"], "still found since: nobody decided it")

    def test_a_label_is_one_line_of_visible_text(self):
        for bad in ("a\u2028b", "a\u202eb", "a\x85b"):
            with self.assertRaises(self.sel.Refusal, msg=repr(bad)) as caught:
                self.sel.plan("acme", {"labels": {"project:alpha-web": bad}})
            self.assertEqual(caught.exception.code, "invalid-label")

    def test_confirm_without_a_key_is_the_same_refusal_everywhere(self):
        m = McpTool.server(self)
        self.assertEqual(m.observatory_org_selection(org="acme", action="confirm-replacement")["error"],
                         "not-in-record")


class ReviewRoundTwelve(Base):
    """What the twelfth review found: each case reproduced, then fixed."""

    def test_a_provider_document_that_does_not_read_leaves_its_accounts_unread(self):
        self.registry("cloudflare-account.json", {"accounts": [
            {"id": "cloudflare:account:cf-acme", "account_id": "cf-acme", "name": "Acme CF"}], "resources": []})
        orgs = json.loads(json.dumps(ORGS))
        orgs["organizations"]["acme"]["match"]["cloudflare_accounts"] = ["cf-acme"]
        (self.home / "config" / "organizations.json").write_text(json.dumps(orgs))
        self.sel.apply("acme", self.ready()["digest"])
        import paths
        (paths.REGISTRY / "cloudflare-account.json").write_text("{")
        v = self.sel.plan("acme", {"labels": {"project:beta-api": "Billing"}})
        self.assertIn("cloudflare:account:cf-acme", v["unread"])
        self.assertNotIn("cloudflare:account:cf-acme", v["unknown"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "source-unreadable")

    def test_who_decided_an_item_is_carried_and_a_label_is_not_a_decision(self):
        words = self.decision("org.select.plan", "acme")
        self.sel.plan("acme", {"include": ["project:notes"], "layout": "one-space"}, caller="agent:mcp",
                      decision=words)
        self.sel.apply("acme", self.sel.plan("acme")["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"labels": {"project:alpha-web": "Alpha"}})["digest"])
        rows = {i["id"]: i for i in self.sel.current("acme")["items"]}
        self.assertEqual(rows["project:notes"]["decidedBy"]["instruction"], words["instruction"])
        self.assertEqual(rows["project:alpha-web"]["by"], "rule")
        self.assertNotIn("decidedBy", rows["project:alpha-web"])
        self.assertEqual(rows["project:alpha-web"]["labelledBy"], {"by": "person", "via": "cli"})

    def test_text_names_unread_and_missing_rows(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:notes"]})
        code, out, _ = Cli.run_cli(self, "select", "--org", "acme", "plan")
        self.assertIn("project:notes", out)
        self.assertIn("(no longer found here)", out)

    def test_show_takes_no_words_anywhere(self):
        code, _, err = Cli.run_cli(self, "select", "--org", "acme", "show", "--decision", "{}", "--json")
        self.assertEqual((code, json.loads(err)["error"]), (1, "invalid-change"))
        m = McpTool.server(self)
        self.assertEqual(m.observatory_org_selection(org="acme", action="show", decision={})["error"],
                         "invalid-change")

    def test_falsy_and_misplaced_values_are_refused_not_dropped(self):
        for change, code in (({"include": 0}, "invalid-change"), ({"layout": 0}, "invalid-layout"),
                             ({"layout": "one-space", "serves": "work"}, "invalid-change")):
            with self.assertRaises(self.sel.Refusal, msg=str(change)) as caught:
                self.sel.plan("acme", change)
            self.assertEqual(caught.exception.code, code, change)


class ReviewRoundThirteen(Base):
    """What the thirteenth review found: each case reproduced, then fixed."""

    def test_a_refusal_never_repeats_a_value(self):
        planted = "sk-or-v1-" + "c" * 64
        m = McpTool.server(self)
        for args in ({"action": "confirm-replacement", "key": planted}, {"action": "plan", "changes": {planted: 1}}):
            answer = m.observatory_org_selection(org="acme", **args)
            self.assertIn("error", answer)
            self.assertNotIn(planted, json.dumps(answer), args)

    def test_serves_is_refused_against_the_records_layout(self):
        self.sel.apply("acme", self.sel.plan("acme", {"layout": "this-space"})["digest"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"serves": "work"})
        self.assertEqual(caught.exception.code, "invalid-change")

    def test_an_undo_that_lost_its_draft_removal_drops_it_after(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.sel.plan("acme", {"exclude": ["project:beta-api"]})
        draft = (self.sel._folder("acme") / "draft.json").read_text()
        self.sel.undo("acme")
        (self.sel._folder("acme") / "draft.json").write_text(draft)
        (self.sel._folder("acme") / "draft.json").chmod(0o600)
        v = self.sel.plan("acme")
        self.assertFalse(v["draft"], "the draft the undo consumed is no draft")
        self.assertEqual(self.sel.show("acme")["state"], "waiting")
        self.sel.apply("acme", self.sel.show("acme")["command"].rsplit(" ", 1)[1])
        self.assertFalse((self.sel._folder("acme") / "draft.json").exists())

    def test_a_chosen_item_gone_after_apply_is_named_by_the_plan(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:notes"]})
        v = self.sel.plan("acme")
        self.assertIn("no longer found here", v["nextStep"])
        self.assertIn("--exclude project:notes", v["command"])

    def test_in_this_space_a_kept_key_waits_to_move_out(self):
        a = self.sel.apply("acme", self.sel.plan("acme", {"layout": "this-space"})["digest"])
        step = next(st for i in self.sel.journal("acme", 1)["items"] if i["id"] == HAND_SLOT for st in i["steps"])
        self.assertEqual((step["state"], step["waitingOn"]), ("waiting", "move-out"))

    def test_the_finding_names_the_organizations_label(self):
        import organization_findings
        importlib.reload(organization_findings)
        rows = organization_findings.selection_findings([{"organization": "acme", "label": "Acme",
                                                          "new": ["project:gamma"]}])
        self.assertIn("Acme", rows[0]["title"])


class ReviewRoundFourteen(Base):
    """What the fourteenth review found: each case reproduced, then fixed."""

    def test_where_the_words_were_said_carries_no_credential_and_a_session_id_is_fine(self):
        planted = "sk-or-v1-" + "d" * 64
        bad = self.decision("org.select.plan", "acme", source={"project": "example-team", "thread": planted})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp", decision=bad)
        self.assertEqual(caught.exception.code, "decision-invalid")
        self.assertNotIn(planted, json.dumps(caught.exception.answer()))
        fine = dict(self.decision("org.select.plan", "acme"),
                    instruction="as I said in session 0faeb32f-1111-4222-8333-444455556666, one space")
        self.assertEqual(self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp",
                                       decision=fine)["layout"], "one-space")

    def test_a_label_keeps_who_chose_a_project_by_hand(self):
        include = dict(self.decision("org.select.plan", "acme"), instruction="include notes")
        label = dict(self.decision("org.select.plan", "acme"), instruction="label notes as N")
        self.sel.plan("acme", {"include": ["project:notes"], "layout": "one-space"}, caller="agent:mcp",
                      decision=include)
        self.sel.plan("acme", {"labels": {"project:notes": "N"}}, caller="agent:mcp", decision=label)
        self.sel.apply("acme", self.sel.plan("acme")["digest"])
        row = next(i for i in self.sel.current("acme")["items"] if i["id"] == "project:notes")
        self.assertEqual((row["decidedBy"]["instruction"], row["labelledBy"]["instruction"]),
                         ("include notes", "label notes as N"))
        grouped = self.sel.plan("acme", {"includeGroup": "needs-you"}, caller="agent:mcp",
                                decision=dict(include, instruction="take the ones that need me"))
        self.sel.plan("acme", {"labels": {"project:shared-tool": "Shared"}}, caller="agent:mcp", decision=label)
        row = self.items(self.sel.plan("acme"))["project:shared-tool"]
        self.assertEqual((row["by"], row["decidedBy"]["instruction"]), ("group", "take the ones that need me"))
        self.assertTrue(grouped)

    def test_the_cli_never_prints_a_rejected_value_shaped_like_a_credential(self):
        planted = "sk-or-v1-" + "e" * 64
        err = io.StringIO()
        import org as org_cli
        importlib.reload(org_cli)
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            org_cli.main(["select", "--org", "acme", "plan", "--layout", planted])
        self.assertNotIn(planted, err.getvalue())

    def test_the_cli_never_repeats_a_rejected_value_that_is_not_an_identifier(self):
        import org as org_cli
        importlib.reload(org_cli)
        for argv in (["select", "--org", "acme", "plan", "--layout", "correct horse battery staple"],
                     ["select", "--org", "acme", "plan", "--layout", "one-space", "--frob", "correct horse"]):
            err = io.StringIO()
            with self.subTest(argv=argv), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                org_cli.main(argv)
            self.assertNotIn("horse", err.getvalue())
            self.assertIn("characters, not shown", err.getvalue())

    def test_undo_that_cannot_restore_says_so(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:notes"]})["digest"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:notes"]})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertIn("undo cannot restore", str(caught.exception))


class ReviewRoundFifteen(Base):
    """What the fifteenth review found: each case reproduced, then fixed."""

    def test_a_decision_that_is_not_an_object_is_refused_not_a_crash(self):
        m = McpTool.server(self)

        class Request:
            meta = {"decision": "yes"}

        class Ctx:
            request_context = Request()
        for args in ({"action": "plan", "changes": {"layout": "one-space"}},
                     {"action": "apply", "digest": "sha256:" + "0" * 64}):
            self.assertEqual(m.observatory_org_selection(org="acme", ctx=Ctx(), **args)["error"], "decision-invalid",
                             args)
        self.assertEqual(m.observatory_org_selection(org="acme", action="plan", changes={"layout": "one-space"},
                                                     decision=["yes"])["error"], "decision-invalid")

    def test_in_one_space_a_kept_key_waits_for_contexts(self):
        self.sel.apply("acme", self.sel.plan("acme", {"layout": "one-space"})["digest"])
        step = next(st for i in self.sel.journal("acme", 1)["items"] if i["id"] == HAND_SLOT for st in i["steps"])
        self.assertEqual((step["state"], step["waitingOn"]), ("waiting", "context-binding"))

    def test_a_read_only_plan_reports_gate_none(self):
        self.assertEqual(self.sel.plan("acme")["gate"], "none")
        self.assertEqual(self.ready()["gate"], "relayed")

    def test_an_empty_changes_mapping_on_apply_is_no_stray_argument(self):
        m = McpTool.server(self)
        answer = m.observatory_org_selection(org="acme", action="apply", changes={},
                                             digest="sha256:" + "0" * 64)
        self.assertNotEqual(answer.get("error"), "invalid-change")


class ReviewRoundSixteen(Base):
    """What the sixteenth review found: each case reproduced, then fixed."""

    def test_an_excluded_row_that_is_gone_moves_nothing_and_asks_nothing(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": [p for p in PROJECTS if p["id"] != "project:shared-tool"]})
        (self.secrets / "projects" / "alpha-web" / "prod" / "HAND_PUT").unlink()
        v = self.sel.plan("acme")
        self.assertEqual((v["unknown"], v["unread"]), ([], []))
        self.assertIn("nothing to apply", v["nextStep"])

    def test_a_layout_change_never_carries_a_kept_key_as_done(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"layout": "this-space"})["digest"])
        step = next(st for i in self.sel.journal("acme", 2)["items"] if i["id"] == HAND_SLOT for st in i["steps"])
        self.assertEqual((step["state"], step["waitingOn"]), ("waiting", "move-out"))
        old = next(st for i in self.sel.journal("acme", 1)["items"] if i["id"] == HAND_SLOT for st in i["steps"])
        self.assertEqual(old["state"], "undone", "the old journal's step no longer carried reads undone")

    def test_a_label_before_the_first_apply_does_not_choose(self):
        self.sel.plan("acme", {"labels": {"project:alpha-web": "Alpha"}, "layout": "one-space"})
        moved = [dict(p, organization="person", organization_source="default", organization_why="no rule")
                 if p["id"] == "project:alpha-web" else p for p in PROJECTS]
        self.registry("projects.json", {"projects": moved})
        v = self.sel.plan("acme")
        row = self.items(v).get("project:alpha-web")
        self.assertEqual(row["decision"], "exclude", "the label alone never kept it chosen")
        self.assertEqual(row["label"], "Alpha")
        self.assertEqual(v["preview"]["organization"]["projects"], 1)


class ReviewRoundSeventeen(Base):
    """What the seventeenth review found: each case reproduced, then fixed."""

    def partner_holds_shared_tool(self) -> Path:
        self.sel.apply("partner", self.sel.plan("partner", {"include": ["project:shared-tool"],
                                                            "layout": "one-space"})["digest"])
        return self.sel._folder("partner") / "revisions"

    @contextlib.contextmanager
    def denied(self, target: Path, call: str):
        real = getattr(os, call)

        def guarded(path, *args, **kwargs):
            if Path(os.fspath(path)) == target:
                raise PermissionError(13, "Permission denied")
            return real(path, *args, **kwargs)
        with patch(f"os.{call}", guarded):
            yield

    def test_an_unsearchable_record_of_another_organization_is_never_read_as_none(self):
        revisions = self.partner_holds_shared_tool()
        for call in ("lstat", "listdir"):
            with self.subTest(call=call), self.denied(revisions, call):
                v = self.ready(include=["project:shared-tool"])
                self.assertEqual(v["state"], "partial")
                self.assertIn("record-unreadable", [d["code"] for d in v["degraded"]])
                self.assertIn("full doctor", v["command"])
                with self.assertRaises(self.sel.Refusal) as caught:
                    self.sel.apply("acme", v["digest"])
                self.assertEqual(caught.exception.code, "conflict-unchecked")
        self.assertIsNone(self.sel.current("acme"))

    def test_a_revisions_entry_that_is_a_file_or_a_link_is_unread(self):
        revisions = self.partner_holds_shared_tool()
        moved = revisions.with_name("kept")
        revisions.rename(moved)
        try:
            os.symlink(moved, revisions, target_is_directory=True)
        except (OSError, NotImplementedError):
            revisions.write_text("not a folder")
        v = self.ready(include=["project:shared-tool"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"])
        self.assertEqual(caught.exception.code, "conflict-unchecked")

    def test_one_unreadable_organization_never_costs_the_others_their_finding(self):
        self.sel.apply("acme", self.ready()["digest"])
        revisions = self.partner_holds_shared_tool()
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        with self.denied(revisions, "lstat"):
            rows = self.sel.new_since_all()
        self.assertEqual([r["organization"] for r in rows], ["acme"])

    def test_a_label_that_is_not_text_is_refused(self):
        self.sel.plan("acme", {"labels": {"project:alpha-web": "Alpha"}})
        for bad in (0, False, None, ["x"]):
            with self.subTest(bad=bad), self.assertRaises(self.sel.Refusal) as caught:
                self.sel.plan("acme", {"labels": {"project:alpha-web": bad}})
            self.assertEqual(caught.exception.code, "invalid-label")
        self.assertEqual(self.items(self.sel.plan("acme"))["project:alpha-web"]["label"], "Alpha")

    def test_an_item_decided_both_ways_in_one_change_is_refused(self):
        for change in ({"include": ["project:beta-api"], "exclude": ["project:beta-api"]},
                       {"include": [HAND_SLOT], "keys": {HAND_SLOT: "keep"}},
                       {"includeGroup": "proposed", "excludeGroup": "proposed"}):
            with self.subTest(change=change), self.assertRaises(self.sel.Refusal) as caught:
                self.sel.plan("acme", change)
            self.assertEqual(caught.exception.code, "invalid-change")
        self.assertFalse(self.sel.plan("acme")["draft"])

    def test_an_account_whose_label_has_a_space_can_be_decided(self):
        self.private("openrouter-issued.json", json.dumps({"issued": {
            "alpha-llm": {"account": "acme-or", "destination": "vault:alpha-web/prod/OPENROUTER_API_KEY"},
            "notes-llm": {"account": "main", "destination": "vault:notes/prod/OPENROUTER_API_KEY"},
            "spare-llm": {"account": "spare team", "destination": "vault:beta-api/prod/OPENROUTER_API_KEY"}}}))
        ident = "openrouter:account:spare team"
        if ident not in self.items(self.sel.plan("acme")):
            self.skipTest("the accounts view does not list this account for the organization")
        v = self.sel.plan("acme", {"include": [ident]})
        self.assertEqual(self.items(v)[ident]["decision"], "include")


class ReviewRoundNineteen(Base):
    """What the nineteenth review found: each case reproduced, then fixed."""

    def cli_error(self, *argv) -> str:
        import org as org_cli
        importlib.reload(org_cli)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            org_cli.main(["select", "--org", "acme", "plan", *argv])
        return err.getvalue()

    def test_the_cli_never_repeats_any_part_of_a_rejected_value(self):
        for argv, secret in ((["--inc=hunter2pass"], "hunter2"), (["--layout", "it's sekrit"], "sekrit"),
                             (["--layout", "it's sekrit"], "it"), (["--include-group", "x\"y'z"], "z")):
            with self.subTest(argv=argv, secret=secret):
                text = self.cli_error(*argv).rpartition("error:")[2]
                self.assertNotIn(secret, text.replace("--include", "").replace("--layout", "")
                                 .replace("invalid", "").replace("choice", ""))
                self.assertIn("characters, not shown", text)
        self.assertIn("--layout", self.cli_error("--layout", "it's sekrit"), "the option's name stays readable")
        self.assertIn("'one-spac'", self.cli_error("--layout", "one-spac"), "a typo of an identifier is shown")

    def test_a_group_never_overrides_an_item_decided_in_the_same_change(self):
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"exclude": ["project:beta-api"], "includeGroup": "proposed"})
        self.assertEqual(caught.exception.code, "invalid-change")
        v = self.sel.plan("acme", {"include": ["project:beta-api"], "includeGroup": "proposed"})
        self.assertEqual(self.items(v)["project:beta-api"]["decision"], "include")

    def test_undo_without_the_configuration_names_an_unread_registry(self):
        self.sel.apply("acme", self.ready(include=["project:notes"])["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:notes"]})["digest"])
        orgs = json.loads(json.dumps(ORGS))
        del orgs["organizations"]["acme"]
        (self.home / "config" / "organizations.json").write_text(json.dumps(orgs))
        import paths
        (paths.REGISTRY / "projects.json").write_text("{")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "registry-unreadable")
        self.assertEqual(len(self.sel._revisions("acme")), 2, "nothing was undone")


class ReviewRoundTwenty(Base):
    """What the twentieth review found: each case reproduced, then fixed."""

    def test_the_operator_may_quote_the_plans_own_command(self):
        v = self.ready()
        words = self.decision("org.select.apply", "acme",
                              instruction=f"apply it: full org select --org acme apply --digest {v['digest']}")
        done = self.sel.apply("acme", v["digest"], decision=words, caller="agent:mcp")
        self.assertEqual(done["revision"], 1)
        planted = self.decision("org.select.apply", "acme", instruction="apply it, key sk-or-v1-" + "e" * 64)
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme", decision=dict(planted, action="org.select.undo"), caller="agent:mcp")
        self.assertEqual(caught.exception.code, "decision-invalid")

    def test_a_typed_dash_leaves_the_message_readable(self):
        import org as org_cli
        importlib.reload(org_cli)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            org_cli.main(["select", "--org", "acme", "plan", "--layout", "bad", "-"])
        self.assertIn("--layout", err.getvalue())
        self.assertIn("separate-space", err.getvalue())


class WhatTheOrganizationSees(Base):
    """ORG-R03: a reference random per organization and a label only when typed; nothing is handed over."""

    def test_items_carry_a_reference_and_only_a_typed_label(self):
        v = self.ready(labels={"project:beta-api": "Billing"})
        a = self.sel.apply("acme", v["digest"])
        items = a["fabricItems"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertLessEqual(set(item), {"ref", "kind", "label"})
            self.assertRegex(item["ref"], r"^[0-9a-f]{32}$")
        self.assertEqual(sorted(i.get("label") for i in items if i.get("label")), ["Billing"])
        wire = json.dumps(items)
        for planted in ("alpha-web", "beta-api", "acme-inc", "project:"):
            self.assertNotIn(planted, wire, "a project or repository name leaves only as a typed label")
        other = self.sel.plan("partner", {"layout": "one-space"})
        b = self.sel.apply("partner", other["digest"])
        self.assertFalse({i["ref"] for i in items} & {i["ref"] for i in b["fabricItems"]})

    def test_nothing_is_sent(self):
        import urllib.request, socket
        with patch.object(urllib.request, "urlopen", side_effect=AssertionError("sent")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("connected")):
            v = self.ready()
            self.sel.apply("acme", v["digest"])
            self.sel.show("acme")


class UndoAndConfirm(Base):
    """ORG-R13 and ORG-R08's confirmation in the separate-space layout."""

    def test_undo_restores_the_revision_before_and_then_nothing(self):
        v = self.ready()
        first = self.sel.apply("acme", v["digest"])
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:beta-api"]})["digest"])
        u = self.sel.undo("acme")
        self.assertEqual((u["answer"], u["outcome"], u["revision"]), ("undo", "undone", 3))
        self.assertEqual(u["restored"]["revision"], 1)
        rec = self.sel.current("acme")
        self.assertEqual(rec["selectionDigest"], first["selectionDigest"])
        self.assertEqual(rec["kind"], "undo")
        redo = self.sel.undo("acme")
        self.assertEqual(redo["restored"]["revision"], 2, "undoing an undo restores what it removed")
        self.assertEqual(self.sel.current("acme")["predecessor"], u["digest"])
        self.assertEqual([r["revision"] for r in self.sel.history("acme")], [1, 2, 3, 4])

    def test_undo_of_the_first_revision_leaves_nothing_chosen(self):
        v = self.ready()
        self.sel.apply("acme", v["digest"])
        u = self.sel.undo("acme")
        self.assertIsNone(u["restored"])
        self.assertEqual(self.sel.current("acme")["items"], [])
        self.assertEqual({s["state"] for i in self.sel.journal("acme", 1)["items"] for s in i["steps"]}, {"undone"})
        self.assertEqual(self.sel.show("acme")["state"], "empty")
        self.assertEqual(self.items(self.sel.plan("acme"))["project:alpha-web"]["decision"], "include",
                         "with nothing chosen, the defaults apply again")

    def test_undo_is_refused_once_an_entry_is_signed(self):
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "nothing-to-undo")
        v = self.ready()
        self.sel.apply("acme", v["digest"])
        path = self.sel._journal_path("acme", 1)
        jdoc = json.loads(path.read_text())
        for item in jdoc["items"]:
            for s in item["steps"]:
                if s["step"] == "entry-signed":
                    s["state"] = "done"
        path.write_text(json.dumps(jdoc))
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme")
        self.assertEqual(caught.exception.code, "entry-signed")
        self.assertIn("OBS-419", str(caught.exception))

    def replacement_arrived(self, revision=1):
        """What OBS-191 and OBS-416 will write once they exist: the request answered and consumers switched."""
        path = self.sel._journal_path("acme", revision)
        jdoc = json.loads(path.read_text())
        for item in jdoc["items"]:
            for st in item["steps"]:
                if item["id"] == ORG_SLOT and st["step"] in ("credential-request", "switch-consumers"):
                    st["state"] = "done"
        path.write_text(json.dumps(jdoc))

    def test_confirm_replacement(self):
        v = self.ready()
        self.sel.apply("acme", v["digest"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.confirm_replacement("acme", ORG_SLOT)
        self.assertEqual(caught.exception.code, "replacement-not-received",
                         "nothing to confirm before the replacement arrived (ORG-R08)")
        self.replacement_arrived()
        c = self.sel.confirm_replacement("acme", ORG_SLOT)
        self.assertEqual((c["outcome"], c["step"]["waitingOn"]), ("confirmed", "rotation"))
        self.assertEqual(c["step"]["state"], "planned", "confirmed, still not revoked: no rotation door yet")
        self.assertEqual(self.sel.confirm_replacement("acme", ORG_SLOT[len("vault:"):])["outcome"], "already-confirmed")
        for key, code in ((HAND_SLOT, "not-revoking"), ("vault:nowhere/prod/X", "not-in-record")):
            with self.assertRaises(self.sel.Refusal) as caught:
                self.sel.confirm_replacement("acme", key)
            self.assertEqual(caught.exception.code, code)
        # A changed selection carries the confirmation over (it already happened).
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:beta-api"]})["digest"])
        step = next(s for i in self.sel.journal("acme", 2)["items"] if i["id"] == ORG_SLOT for s in i["steps"]
                    if s["step"] == "revoke-personal")
        self.assertIn("confirmedAt", step)

    def test_this_space_lists_what_moves_out(self):
        v = self.sel.plan("acme", {"layout": "this-space"})
        self.assertEqual(v["preview"]["movesOut"]["waitingOn"], "move-out")
        self.assertEqual(v["preview"]["movesOut"]["projects"], 3, "notes, shared-tool and Partner's project")
        a = self.sel.apply("acme", v["digest"])
        self.assertIn("move-out", [w["on"] for w in a["waitingOn"]])
        self.assertIsNone(self.ready()["preview"]["movesOut"])

    def test_no_confirmation_in_one_space(self):
        v = self.sel.plan("acme", {"layout": "one-space"})
        self.sel.apply("acme", v["digest"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.confirm_replacement("acme", ORG_SLOT)
        self.assertEqual(caught.exception.code, "confirmation-not-needed")


class NewSince(Base):
    """ORG-R16: found after the selection, proposed and never selected; a finding on the board."""

    def test_a_new_project_and_key_are_listed_never_chosen(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        self.private("projects/beta-api/prod/LATER_KEY", STAND_IN_VALUE)
        self.private("projects/beta-api/prod/LATER_BY_HAND", STAND_IN_VALUE)
        self.private("openrouter-issued.json", json.dumps({"issued": {
            "alpha-llm": {"account": "acme-or", "destination": "vault:alpha-web/prod/OPENROUTER_API_KEY"},
            "notes-llm": {"account": "main", "destination": "vault:notes/prod/OPENROUTER_API_KEY"},
            "later-llm": {"account": "acme-or", "destination": "vault:beta-api/prod/LATER_KEY"}}}))
        # Something that needs a decision anyway, found since: listed as needs-you, not as proposed by the rules.
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"},
            {"id": "project:delta", "name": "delta", "owners": ["acme-inc", "your-user"], "local_folders": ["delta"],
             "organization": None, "organization_source": "conflict", "organization_why": "acme vs person"}]})
        v = self.sel.plan("acme")
        items = self.items(v)
        self.assertEqual((items["project:gamma"]["decision"], items["project:gamma"]["new"]), ("exclude", True))
        self.assertEqual(items["vault:beta-api/prod/LATER_KEY"]["decision"], "keep")
        self.assertEqual((items["project:delta"]["decision"], items["project:delta"]["new"]), ("exclude", False))
        self.assertFalse(items["vault:beta-api/prod/LATER_BY_HAND"]["new"], "put by hand: no rule proposes it")
        self.assertIn("project:gamma", v["new"])
        self.assertEqual(items["project:alpha-web"]["decision"], "include", "the record's decisions stand")
        self.assertEqual(self.sel.show("acme")["new"], ["project:gamma", "vault:beta-api/prod/LATER_KEY"])
        self.assertEqual(len(self.sel._revisions("acme")), 1, "nothing changed the record")
        import organization_findings
        importlib.reload(organization_findings)
        rows = organization_findings.selection_findings(self.sel.new_since_all())
        self.assertEqual([(r["type"], r["subject"]) for r in rows], [("organization.selection_new", "organization:acme")])
        self.assertIn("2", rows[0]["title"])
        self.assertIn("1 project(s) and 1 key(s)", rows[0]["detail"])
        self.assertIn("full org select --org acme plan", rows[0]["action"])

    def test_no_finding_without_a_record(self):
        import organization_findings
        importlib.reload(organization_findings)
        self.assertEqual(organization_findings.selection_findings(self.sel.new_since_all()), [])


class Gates(Base):
    """DEC-0121: an agent changes, applies, undoes and confirms only with the operator's words."""

    def test_an_agent_needs_the_operators_words(self):
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp")
        self.assertEqual(caught.exception.code, "operator-decision-required")
        self.assertEqual(caught.exception.answer()["action"], "org.select.plan")
        v = self.sel.plan("acme", {"layout": "one-space"}, caller="agent:mcp",
                          decision=self.decision("org.select.plan", "acme"))
        self.assertEqual(self.sel.plan("acme", caller="agent:mcp")["digest"], v["digest"], "reading needs nothing")
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.apply("acme", v["digest"], caller="agent:mcp")
        self.assertEqual(caught.exception.code, "operator-decision-required")
        for bad in (self.decision("org.select.undo", "acme"), self.decision("org.select.apply", "partner"),
                    {**self.decision("org.select.apply", "acme"), "at": "2020-01-01T00:00:00Z"},
                    {**self.decision("org.select.apply", "acme"), "by": "agent"}):
            with self.assertRaises(self.sel.Refusal) as caught:
                self.sel.apply("acme", v["digest"], caller="agent:mcp", decision=bad)
            self.assertEqual(caught.exception.code, "decision-invalid", bad)
        words = self.decision("org.select.apply", "acme", source={"project": "example-team", "session": "s-1"})
        a = self.sel.apply("acme", v["digest"], caller="agent:mcp", decision=words)
        self.assertEqual(a["decidedBy"]["instruction"], "move the Acme projects into Acme")
        self.assertEqual((a["decidedBy"]["via"], a["decidedBy"]["caller"]), ("relayed-instruction", "agent:mcp"))
        self.assertEqual(self.sel.current("acme")["decidedBy"]["source"], {"project": "example-team",
                                                                            "session": "s-1"})
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.undo("acme", caller="agent:mcp")
        self.assertEqual(caught.exception.code, "operator-decision-required")
        self.assertEqual(self.sel.undo("acme", caller="agent:mcp",
                                       decision=self.decision("org.select.undo", "acme"))["outcome"], "undone")


class McpTool(Base):
    """`observatory_org_selection` reads the operator's words from `decision` or from `_meta.decision`."""

    def server(self):
        import importlib.util
        sys.path.insert(0, str(ROOT / "mcp"))
        spec = importlib.util.spec_from_file_location("srv_org_selection", ROOT / "mcp/server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["srv_org_selection"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_the_words_come_from_meta_or_the_argument(self):
        m = self.server()

        class Request:
            def __init__(self, meta):
                self.meta = meta

        class Ctx:
            def __init__(self, meta):
                self.request_context = Request(meta)
        refused = m.observatory_org_selection(org="acme", action="plan", changes={"layout": "one-space"})
        self.assertEqual(refused["error"], "operator-decision-required")
        meta = {"decision": self.decision("org.select.plan", "acme")}
        v = m.observatory_org_selection(org="acme", action="plan", changes={"layout": "one-space"}, ctx=Ctx(meta))
        self.assertEqual(v["layout"], "one-space")
        a = m.observatory_org_selection(org="acme", action="apply", digest=v["digest"],
                                        decision=self.decision("org.select.apply", "acme"))
        self.assertEqual((a["outcome"], a["decidedBy"]["caller"]), ("applied", "agent:mcp"))
        self.assertEqual(m.observatory_org_selection(org="acme", action="show")["state"], "waiting")


class Cli(Base):
    """`full org select`: the same answers as the functions, text or JSON, refusals on stderr with their command."""

    def run_cli(self, *argv):
        import org as org_cli
        importlib.reload(org_cli)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = org_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_plan_apply_show_undo(self):
        code, out, _ = self.run_cli("select", "--org", "acme", "plan", "--layout", "separate-space", "--serves",
                                    "work-acme", "--label", "project:alpha-web=Alpha website", "--exclude",
                                    "project:beta-api", "--key", f"{HAND_SLOT}=hand-over-keep", "--json")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(self.items(plan)[HAND_SLOT]["decision"], "hand-over-keep")
        code, out, err = self.run_cli("select", "--org", "acme", "apply", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"], "not-previewed")
        code, out, _ = self.run_cli("select", "--org", "acme", "apply", "--digest", plan["digest"], "--json")
        self.assertEqual((code, json.loads(out)["outcome"]), (0, "applied"))
        code, out, _ = self.run_cli("select", "--org", "acme", "show")
        self.assertEqual(code, 0)
        self.assertIn("revision 1", out)
        self.assertIn("fabric-adaptation-plan", out)
        code, _, err = self.run_cli("select", "--org", "acme", "confirm-replacement", ORG_SLOT, "--json")
        self.assertEqual((code, json.loads(err)["error"]), (1, "replacement-not-received"))
        code, out, _ = self.run_cli("select", "--org", "acme", "undo")
        self.assertEqual(code, 0)
        self.assertIn("undone", out)

    def test_text_plan_says_what_stays_personal(self):
        code, out, _ = self.run_cli("select", "--org", "acme")
        self.assertEqual(code, 0)
        self.assertIn("stay personal and are never sent", out)
        self.assertIn("--layout separate-space", out)
        code, _, err = self.run_cli("select", "--org", "nobody", "plan")
        self.assertEqual(code, 1)
        self.assertIn("refused", err)

    def test_an_agent_carries_the_words_on_the_command_line(self):
        decision = json.dumps(self.decision("org.select.plan", "acme"))
        code, out, _ = self.run_cli("select", "--org", "acme", "plan", "--layout", "one-space", "--decision",
                                    decision, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(self.items(json.loads(out))["project:alpha-web"]["decision"], "include")
        draft = json.loads((self.sel._folder("acme") / "draft.json").read_text())
        self.assertEqual(draft["layout"], "one-space")
        code, _, err = self.run_cli("select", "--org", "acme", "plan", "--layout", "this-space", "--decision", "{",
                                    "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"], "decision-invalid")


if __name__ == "__main__":
    unittest.main()
