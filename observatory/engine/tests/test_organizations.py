#!/usr/bin/env python3
"""Organizations: whose accounts a project uses, and the resources recorded for it.

Synthetic workspace only; the organizations, accounts and projects below are
invented and name no real estate.
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
sys.path.append(str(ROOT / "tools"))
import workspace

ORGS = {
    "organizations": {
        "company": {"label": "Example Company", "ga4_account": "accounts/100",
                    "figma": {"team": "Company Design", "project": "Builder"},
                    "match": {"repository_owners": ["company-org", "company-bb"], "products": ["product:suite"]}},
        "person": {"label": "Example Person", "default": True, "ga4_account": "accounts/200",
                   "ga4_legacy_accounts": {"accounts/300": ["properties/31"]},
                   "figma": {"team": "Personal"}},
    },
    "projects": {"project:partner-app": {"organization": "person", "why": "the operator: part of the person's product"}},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home)}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        self.write("organizations.json", ORGS)
        self.write("ownership.json", {"organizations": ["company-org", "person-org"], "work_organizations": ["company-bb"]})
        self.write("products.json", {"products": {"product:suite": {"members": {"project:suite-bot": "bot"}}}})
        import paths
        importlib.reload(paths)
        import organizations
        self.orgs = importlib.reload(organizations)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, name, doc):
        (self.home / "config" / name).write_text(json.dumps(doc))

    def assign(self, **project):
        project.setdefault("owners", [])
        return self.orgs.Assigner().assign(project)


class Assignment(Base):
    def test_order_of_answers(self):
        self.assertEqual(self.assign(id="project:partner-app", owners=["stranger"])["source"], "declared")
        self.assertEqual(self.assign(id="project:x", owners=["company-org"], organization="person")["organization"], "person")
        rule = self.assign(id="project:x", owners=["company-bb"])
        self.assertEqual((rule["organization"], rule["source"]), ("company", "rule"))
        self.assertIn("company-bb", rule["why"])
        self.assertEqual(self.assign(id="project:suite-bot", owners=["person-org"])["organization"], "company")
        self.assertEqual(self.assign(id="project:suite-bot")["organization"], "company")
        ext = self.assign(id="project:clone", owners=["somebody-else"])
        self.assertEqual((ext["organization"], ext["source"]), ("external", "external"))
        self.assertEqual(self.assign(id="project:mine", owners=["person-org"])["source"], "default")
        self.assertEqual(self.assign(id="project:folder-only")["organization"], "person")

    def test_conflict_names_both_and_assigns_neither(self):
        got = self.assign(id="project:suite-bot", owners=["person-org", "company-org"])
        # company matches twice (owner and product); person matches nothing: not a conflict.
        self.assertEqual(got["organization"], "company")
        self.write("organizations.json", {**ORGS, "organizations": {**ORGS["organizations"], "person": {
            **ORGS["organizations"]["person"], "match": {"repository_owners": ["person-org"]}}}})
        got = self.orgs.Assigner().assign({"id": "project:suite-bot", "owners": ["person-org"]})
        self.assertEqual((got["organization"], got["source"]), (None, "conflict"))
        self.assertIn("company", got["why"]); self.assertIn("person", got["why"])

    def test_unconfigured_emits_nothing(self):
        self.write("organizations.json", {"organizations": {}, "projects": {}})
        self.assertIsNone(self.orgs.Assigner().assign({"id": "project:x", "owners": ["company-org"]}))

    def test_validate(self):
        self.assertEqual(self.orgs.validate(ORGS), [])
        bad = json.loads(json.dumps(ORGS))
        bad["organizations"]["company"]["default"] = True
        bad["organizations"]["company"]["ga4_account"] = "100"
        bad["projects"]["project:y"] = {"organization": "nobody"}
        problems = " | ".join(self.orgs.validate(bad))
        self.assertIn("exactly one", problems)
        self.assertIn("accounts/<number>", problems)
        self.assertIn("project:y", problems)

    def test_destinations(self):
        d = self.orgs.Assigner().destinations("company")
        self.assertEqual(d, {"label": "Example Company", "ga4Account": "accounts/100",
                             "figmaTeam": "Company Design", "figmaProject": "Builder"})


class Resources(Base):
    def test_resource_shape(self):
        ok = {"kind": "ga4-property", "identifier": "properties/1", "account": "accounts/200"}
        self.assertEqual(self.orgs.resource_problem(ok), "")
        self.assertIn("kind", self.orgs.resource_problem({**ok, "kind": "spreadsheet"}))
        self.assertIn("identifier", self.orgs.resource_problem({**ok, "identifier": " "}))
        self.assertIn("unknown", self.orgs.resource_problem({**ok, "token": "x"}))

    def test_merge_appends_and_updates_never_drops(self):
        a = {"kind": "server", "identifier": "api.example.test"}
        b = {"kind": "gcp-project", "identifier": "example-1", "account": "org-1"}
        merged = self.orgs.merge_resources([a], [b])
        self.assertEqual(len(merged), 2)
        moved = self.orgs.merge_resources(merged, [{**b, "account": "org-2"}])
        self.assertEqual(len(moved), 2)
        self.assertEqual(next(r for r in moved if r["kind"] == "gcp-project")["account"], "org-2")

    def test_proposal_accepts_structured_fields_on_an_empty_curation_file(self):
        import proposals
        importlib.reload(proposals)
        ev = [{"uri": "https://example.test/receipt"}]
        good = {"resources": [{"kind": "firebase-project", "identifier": "example-app"}]}
        self.assertEqual(proposals.refusal("project:x", good, ev), "")
        self.assertEqual(proposals.refusal("project:x", {"organization": "company"}, ev), "")
        self.assertIn("kind", proposals.refusal("project:x", {"resources": [{"kind": "x", "identifier": "y"}]}, ev))
        self.assertIn("non-empty", proposals.refusal("project:x", {"resources": []}, ev))
        self.assertIn("organization must be one of", proposals.refusal("project:x", {"organization": "nobody"}, ev))
        self.assertIn("evidence", proposals.refusal("project:x", good, []))

    def test_review_accept_appends_resources(self):
        import review
        importlib.reload(review)
        from store import db
        importlib.reload(db)
        conn = db.connect()
        f = self.home / "config" / "project_overrides.json"
        f.write_text(json.dumps({"projects": {"project:x": {"resources": [{"kind": "server", "identifier": "a.example.test"}],
                                                            "why": "earlier"}}}))
        for pid, ident in (("p-1", "b.example.test"), ("p-2", "c.example.test")):
            conn.execute("INSERT INTO proposals(id,target_id,patch_json,evidence_json,status,created_at) VALUES (?,?,?,?,?,?)",
                         (pid, "project:x", json.dumps({"resources": [{"kind": "server", "identifier": ident}]}),
                          json.dumps({"owner": "agent:test", "evidence": [{"uri": "https://example.test"}]}),
                          "proposed", "2026-01-01T00:00:00Z"))
        conn.commit()
        with patch.object(review, "require_terminal", lambda *_: None):
            for pid in ("p-1", "p-2"):
                review.cmd_proposal_accept(conn, type("A", (), {"id": pid, "why": "operator accepted"})())
        rows = json.loads(f.read_text())["projects"]["project:x"]["resources"]
        self.assertEqual(sorted(r["identifier"] for r in rows), ["a.example.test", "b.example.test", "c.example.test"])
        conn.close()


class Analytics(Base):
    PROJECTS = [
        {"id": "project:app", "organization": "person", "organization_source": "default", "organization_why": "no rule"},
        {"id": "project:co", "organization": "company", "organization_source": "rule", "organization_why": "owner"},
        {"id": "project:x", "organization": "external", "organization_source": "external"},
    ]

    def doc(self, props, accounts=("accounts/100", "accounts/200", "accounts/300")):
        return {"properties": props, "accounts": [{"account": a} for a in accounts]}

    def run_rules(self, doc):
        import google_findings
        importlib.reload(google_findings)
        return google_findings.organization_findings(doc, self.PROJECTS, ORGS)

    def test_wrong_account_flagged_right_account_silent(self):
        out = self.run_rules(self.doc([
            {"property": "properties/1", "name": "App", "project": "project:app", "account": "accounts/100"},
            {"property": "properties/2", "name": "Co", "project": "project:co", "account": "accounts/100"},
            {"property": "properties/3", "name": "Clone", "project": "project:x", "account": "accounts/100"}]))
        wrong = [f for f in out if f["type"] == "analytics.property_wrong_account"]
        self.assertEqual([f["subject"] for f in wrong], ["project:app"])
        self.assertIn("accounts/200", wrong[0]["action"])

    def test_new_property_in_legacy_account(self):
        out = self.run_rules(self.doc([
            {"property": "properties/31", "name": "Old", "account": "accounts/300"},
            {"property": "properties/32", "name": "New", "account": "accounts/300"}]))
        legacy = [f for f in out if f["type"] == "analytics.legacy_account_property"]
        self.assertEqual(len(legacy), 1)
        self.assertIn("New", legacy[0]["detail"]); self.assertNotIn("Old", legacy[0]["detail"])

    def test_unreadable_organization_account(self):
        out = self.run_rules(self.doc([], accounts=("accounts/200",)))
        self.assertEqual([f["title"] for f in out if f["type"] == "analytics.organization_account_unreadable"],
                         ["Example Company's analytics account accounts/100 is not readable"])

    def test_silent_without_configuration(self):
        import google_findings
        self.assertEqual(google_findings.organization_findings(self.doc([{"property": "p", "account": "a"}]), self.PROJECTS, {}), [])


class Surfaces(Base):
    def line(self, project, locale="en"):
        import session_start
        importlib.reload(session_start)
        sys.path.insert(0, str(ROOT / "dashboard"))
        import i18n
        return session_start.organization_line(project, i18n.Translator(locale))

    def test_session_start_names_destination_and_duty(self):
        text = self.line({"id": "project:app", "organization": "company", "organization_source": "rule",
                          "resources": [{"kind": "server", "identifier": "a"}]})
        self.assertTrue(text.startswith("\n"))
        for part in ("Example Company", "GA accounts/100", "Figma Company Design/Builder",
                     "resources recorded: 1", 'observatory_propose {"resources": [...]}'):
            self.assertIn(part, text)

    def test_session_start_external_conflict_absent_and_russian(self):
        self.assertIn("create nothing", self.line({"id": "p", "organization": "external", "organization_source": "external"}))
        self.assertIn("ask the operator", self.line({"id": "p", "organization": None, "organization_source": "conflict",
                                                     "organization_why": "a vs b"}))
        self.assertEqual(self.line({"id": "p"}), "")
        ru = self.line({"id": "p", "organization": "person", "organization_source": "default"}, "ru")
        self.assertIn("владелец: Example Person", ru)
        self.assertIn("учтено ресурсов: 0", ru)

    def test_project_view_carries_organization_and_resources(self):
        import survey
        importlib.reload(survey)
        view = survey._project_view({"id": "project:app", "name": "app", "ownership": "owned", "lifecycle": "active",
                                     "organization": "company", "organization_source": "rule", "organization_why": "owner",
                                     "resources": [{"kind": "gcp-project", "identifier": "g-1"}]}, {}, {})
        self.assertEqual(view["organization"]["ga4Account"], "accounts/100")
        self.assertEqual(view["organization"]["source"], "rule")
        self.assertEqual(view["resources"], [{"kind": "gcp-project", "identifier": "g-1"}])
        bare = survey._project_view({"id": "project:b", "name": "b", "ownership": "owned", "lifecycle": "active"}, {}, {})
        self.assertNotIn("organization", bare)


if __name__ == "__main__":
    unittest.main()
