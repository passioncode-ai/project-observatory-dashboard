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
ORG_KEY = "vault:alpha-web/prod/OPENROUTER_API_KEY"
HAND_KEY = "vault:alpha-web/prod/HAND_PUT"
NOTES_KEY = "vault:notes/prod/OPENROUTER_API_KEY"


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

    def seal_values(self):
        """Every vault value file unreadable to its own owner: anything that opened one would fail on it."""
        for path in (self.secrets / "projects").rglob("*"):
            if path.is_file() and not path.name.endswith((".json", ".jsonl")):
                path.chmod(0)

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
        self.assertEqual(items[ORG_KEY]["decision"], "hand-over-revoke",
                         "project and issuer agree: handed over and revoked once the replacement works")
        self.assertEqual(items[HAND_KEY]["decision"], "keep", "put by hand: ambiguous, never chosen by default")
        self.assertEqual(items["openrouter:account:acme-or"]["decision"], "include")
        self.assertIsNone(v["layout"])
        self.assertEqual(v["recommendedLayout"], "separate-space")
        self.assertIn("--layout separate-space", v["command"], "the layout is chosen before anything (ORG-R29)")
        p = v["preview"]
        self.assertEqual(p["organization"]["projects"], 2)
        self.assertEqual((p["keysRequested"], p["keysRevoked"]), (1, 1))
        self.assertEqual(p["staysPersonal"]["projects"], 3, "notes, shared-tool, partner-app; Not ours is not counted")
        self.assertEqual(p["staysPersonal"]["keys"], 3)
        self.assertIn("3 projects and 3 keys stay personal and are never sent", p["staysPersonal"]["line"])
        self.assertEqual([c for c in p["cannotBeUndone"] if ORG_KEY in c][:1] != [], True)
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
                                   "keys": {HAND_KEY: "hand-over-keep"}, "layout": "separate-space",
                                   "serves": "work-acme"})
        items = self.items(v)
        self.assertEqual((items["project:notes"]["decision"], items["project:notes"]["by"]), ("include", "hand"))
        self.assertEqual(items["project:beta-api"]["decision"], "exclude")
        self.assertEqual(items["project:alpha-web"]["label"], "Alpha website")
        self.assertEqual(items[HAND_KEY]["decision"], "hand-over-keep")
        self.assertIn(NOTES_KEY, items, "a project chosen by hand brings its keys into the plan (ORG-W6)")
        self.assertEqual(items[NOTES_KEY]["decision"], "keep", "its issuer is personal: never chosen by default")
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
        self.assertEqual(self.items(v)[ORG_KEY]["decision"], "keep")
        v = self.sel.plan("acme", {"keys": {ORG_KEY: "hand-over-keep"}})
        self.assertEqual(self.items(v)[ORG_KEY]["decision"], "hand-over-keep")

    def test_bad_changes_are_refused_and_change_nothing(self):
        for change, code in (({"include": ["project:nowhere"]}, "unknown-item"),
                             ({"include": ["not an id"]}, "unknown-item"),
                             ({"keys": {ORG_KEY: "copy"}}, "invalid-choice"),
                             ({"keys": {"project:alpha-web": "keep"}}, "unknown-item"),
                             ({"labels": {"project:alpha-web": "line\nbreak"}}, "invalid-label"),
                             ({"labels": {"project:alpha-web": "x" * 81}}, "invalid-label"),
                             ({"labels": {ORG_KEY: "x"}}, "unknown-item"),
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
        self.seal_values()
        v = self.ready(labels={"project:alpha-web": "Alpha website"})
        a = self.sel.apply("acme", v["digest"])
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
        self.assertEqual(steps[ORG_KEY]["credential-request"]["state"], "waiting")
        self.assertEqual(steps[ORG_KEY]["revoke-personal"]["waitingOn"], "replacement-confirmation")
        self.assertTrue(steps[ORG_KEY]["revoke-personal"]["cannotBeUndone"])
        self.assertEqual(steps[HAND_KEY]["stays-personal"]["state"], "done")
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
        self.assertEqual(again["outcome"], "unchanged")
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
        self.assertIn("unassign", caught.exception.command)

    def test_confirm_replacement(self):
        v = self.ready()
        self.sel.apply("acme", v["digest"])
        c = self.sel.confirm_replacement("acme", ORG_KEY)
        self.assertEqual((c["outcome"], c["step"]["waitingOn"]), ("confirmed", "rotation"))
        self.assertEqual(c["step"]["state"], "planned", "confirmed, still not revoked: no rotation door yet")
        self.assertEqual(self.sel.confirm_replacement("acme", ORG_KEY[len("vault:"):])["outcome"], "already-confirmed")
        for key, code in ((HAND_KEY, "not-revoking"), ("vault:nowhere/prod/X", "not-in-record")):
            with self.assertRaises(self.sel.Refusal) as caught:
                self.sel.confirm_replacement("acme", key)
            self.assertEqual(caught.exception.code, code)
        # A changed selection carries the confirmation over (it already happened).
        self.sel.apply("acme", self.sel.plan("acme", {"exclude": ["project:beta-api"]})["digest"])
        step = next(s for i in self.sel.journal("acme", 2)["items"] if i["id"] == ORG_KEY for s in i["steps"]
                    if s["step"] == "revoke-personal")
        self.assertIn("confirmedAt", step)

    def test_this_space_lists_what_moves_out(self):
        v = self.sel.plan("acme", {"layout": "this-space"})
        self.assertEqual(v["preview"]["movesOut"]["waitingOn"], "move-out")
        self.assertEqual(v["preview"]["movesOut"]["projects"], 3)
        a = self.sel.apply("acme", v["digest"])
        self.assertIn("move-out", [w["on"] for w in a["waitingOn"]])
        self.assertIsNone(self.ready()["preview"]["movesOut"])

    def test_no_confirmation_in_one_space(self):
        v = self.sel.plan("acme", {"layout": "one-space"})
        self.sel.apply("acme", v["digest"])
        with self.assertRaises(self.sel.Refusal) as caught:
            self.sel.confirm_replacement("acme", ORG_KEY)
        self.assertEqual(caught.exception.code, "confirmation-not-needed")


class NewSince(Base):
    """ORG-R16: found after the selection, proposed and never selected; a finding on the board."""

    def test_a_new_project_and_key_are_listed_never_chosen(self):
        self.sel.apply("acme", self.ready()["digest"])
        self.registry("projects.json", {"projects": PROJECTS + [
            {"id": "project:gamma", "name": "gamma", "owners": ["acme-inc"], "local_folders": ["gamma"],
             "organization": "acme", "organization_source": "rule", "organization_why": "repository owner acme-inc"}]})
        self.private("projects/beta-api/prod/LATER_KEY", STAND_IN_VALUE)
        v = self.sel.plan("acme")
        items = self.items(v)
        self.assertEqual((items["project:gamma"]["decision"], items["project:gamma"]["new"]), ("exclude", True))
        self.assertEqual(items["vault:beta-api/prod/LATER_KEY"]["decision"], "keep")
        self.assertIn("project:gamma", v["new"])
        self.assertEqual(items["project:alpha-web"]["decision"], "include", "the record's decisions stand")
        self.assertEqual(self.sel.show("acme")["new"], ["project:gamma", "vault:beta-api/prod/LATER_KEY"])
        self.assertEqual(len(self.sel._revisions("acme")), 1, "nothing changed the record")
        import organization_findings
        importlib.reload(organization_findings)
        rows = organization_findings.selection_findings(self.sel.new_since_all())
        self.assertEqual([(r["type"], r["subject"]) for r in rows], [("organization.selection_new", "organization:acme")])
        self.assertIn("2", rows[0]["title"])
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
                                    "project:beta-api", "--key", f"{HAND_KEY}=hand-over-keep", "--json")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(self.items(plan)[HAND_KEY]["decision"], "hand-over-keep")
        code, out, err = self.run_cli("select", "--org", "acme", "apply", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"], "not-previewed")
        code, out, _ = self.run_cli("select", "--org", "acme", "apply", "--digest", plan["digest"], "--json")
        self.assertEqual((code, json.loads(out)["outcome"]), (0, "applied"))
        code, out, _ = self.run_cli("select", "--org", "acme", "show")
        self.assertEqual(code, 0)
        self.assertIn("revision 1", out)
        self.assertIn("fabric-adaptation-plan", out)
        code, out, _ = self.run_cli("select", "--org", "acme", "confirm-replacement", ORG_KEY, "--json")
        self.assertEqual((code, json.loads(out)["outcome"]), (0, "confirmed"))
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
