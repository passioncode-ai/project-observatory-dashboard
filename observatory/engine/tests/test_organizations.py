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
        self.assertIn("организация: Example Person", ru)
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


TEAM = "11111111-1111-4111-8111-111111111111"
SHARED_TEAM = "22222222-2222-4222-8222-222222222222"


def do_doc(infra_registry):
    """`registry/digitalocean-account.json` as `infra_registry` builds it: one app linked to
    project:shop through a repository the registry holds, one app linked to nothing, one team shared."""
    scan = {"scanned_on": "2026-11-08", "degraded": [], "teams": [
        {"label": "company-team", "team_uuid": TEAM, "team_name": "Company Team", "token_kind": "stash",
         "apps": [{"id": "a1", "name": "shop-api", "serving": True, "repositories": ["shop-org/shop"]},
                  {"id": "a2", "name": "suite-bot", "serving": True, "repositories": []}]},
        {"label": "shared-team", "team_uuid": SHARED_TEAM, "team_name": "Shared Team", "token_kind": "stash",
         "apps": [{"id": "a3", "name": "both-api", "serving": True, "repositories": ["shop-org/both"]}]}]}
    repos = [{"id": "repository:shop-org/shop", "name_with_owner": "shop-org/shop", "host": "github"},
             {"id": "repository:shop-org/both", "name_with_owner": "shop-org/both", "host": "github"}]
    relations = [{"type": "implemented_by", "from": "project:shop", "to": "repository:shop-org/shop"},
                 {"type": "implemented_by", "from": "project:both", "to": "repository:shop-org/both"}]
    return infra_registry.digitalocean_document(scan, repos, relations, "2026-11-08")


class ProviderAccounts(Base):
    """OBS-412 part 1: `digitalocean_teams`, `cloudflare_accounts` and `heroku_teams` (ORG-R04)."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "collectors"))
        import infra_registry
        self.infra = infra_registry
        orgs = json.loads(json.dumps(ORGS))
        orgs["organizations"]["company"]["match"].update(
            digitalocean_teams=["Company Team", SHARED_TEAM], cloudflare_accounts=["acc-company"],
            heroku_teams=["company-heroku"])
        orgs["organizations"]["person"]["match"] = {"digitalocean_teams": [SHARED_TEAM]}
        self.write("organizations.json", orgs)
        self.config = orgs

    def test_the_new_rules_validate_and_an_unknown_one_is_named(self):
        self.assertEqual(self.orgs.validate(self.config), [])
        bad = json.loads(json.dumps(self.config))
        bad["organizations"]["company"]["match"]["aws_accounts"] = ["1"]
        bad["organizations"]["company"]["match"]["heroku_teams"] = "company-heroku"
        problems = " | ".join(self.orgs.validate(bad))
        self.assertIn("match.aws_accounts is not a known list rule", problems)
        self.assertIn("match.heroku_teams is not a known list rule", problems)

    def test_an_account_row_says_whose_it_is_and_a_shared_one_is_nobodys(self):
        a = self.orgs.Assigner()
        self.assertEqual(a.account("digitalocean", [TEAM, "Company Team"])["organization"], "company")
        self.assertEqual(a.account("digitalocean", ["x", "company team"])["organization"], "company",
                         "a team name matches in any case")
        shared = a.account("digitalocean", [SHARED_TEAM, "Shared Team"])
        self.assertEqual((shared["organization"], shared["organization_source"]), (None, "conflict"))
        self.assertIn("company and person", shared["organization_why"])
        none = a.account("cloudflare", ["acc-other"])
        self.assertEqual((none["organization"], none["organization_source"]), (None, "none"),
                         "an account no rule names is undecided, never the default")
        self.assertEqual(a.account("cloudflare", ["acc-company"])["organization"], "company")
        self.assertEqual(a.account("heroku", ["company-heroku"])["organization"], "company")
        self.assertEqual(a.account("heroku", ["acc-company"])["organization"], None,
                         "a rule names accounts of its own provider only")

    def test_a_project_is_assigned_only_through_resources_the_registry_ties_to_it(self):
        doc = do_doc(self.infra)
        accounts = self.orgs.project_accounts(digitalocean=doc)
        self.assertEqual(sorted(accounts), ["project:both", "project:shop"],
                         "suite-bot names no repository: its team is no evidence for any project")
        self.assertEqual(accounts["project:shop"], [{"provider": "digitalocean", "keys": [TEAM, "Company Team"],
                                                     "label": "Company Team", "via": "app shop-api"}])
        a = self.orgs.Assigner()
        got = a.assign({"id": "project:shop", "owners": ["shop-org"]}, accounts["project:shop"])
        self.assertEqual((got["organization"], got["source"]), ("company", "rule"))
        self.assertIn("DigitalOcean team Company Team (app shop-api)", got["why"])
        both = a.assign({"id": "project:both", "owners": ["shop-org"]}, accounts["project:both"])
        self.assertEqual((both["organization"], both["source"]), (None, "conflict"),
                         "a project in a shared team is decided per project, never guessed")
        self.assertEqual(a.assign({"id": "project:shop", "owners": ["shop-org"]})["source"], "external",
                         "without the accounts the answer is what it was")

    def test_cloudflare_and_heroku_reach_the_project_the_same_way(self):
        cf = {"accounts": [{"id": "cloudflare:account:acc-company", "account_id": "acc-company", "name": "Co"}],
              "resources": [{"id": "cloudflare:pages:acc-company/site", "kind": "pages", "name": "site",
                             "project": "project:site", "link_rule": "pages-source-repo",
                             "account_ref": "cloudflare:account:acc-company"},
                            {"id": "cloudflare:worker:acc-company/w", "kind": "worker", "name": "w", "project": None,
                             "link_rule": None, "account_ref": "cloudflare:account:acc-company"}]}
        heroku = [{"id": "heroku:api", "name": "api", "team": "company-heroku", "project": "project:api",
                   "link_rule": "heroku-github-repo"},
                  {"id": "heroku:mine", "name": "mine", "team": "personal", "project": "project:mine",
                   "link_rule": "heroku-github-repo"},
                  {"id": "heroku:loose", "name": "loose", "team": "company-heroku", "project": None,
                   "link_rule": None}]
        accounts = self.orgs.project_accounts(cloudflare=cf, heroku_apps=heroku)
        self.assertEqual(sorted(accounts), ["project:api", "project:site"])
        a = self.orgs.Assigner()
        self.assertEqual(a.assign({"id": "project:site"}, accounts["project:site"])["organization"], "company")
        self.assertIn("Heroku team company-heroku (app api)",
                      a.assign({"id": "project:api"}, accounts["project:api"])["why"])

    def test_reassign_changes_only_undeclared_projects_and_says_which(self):
        doc = do_doc(self.infra)
        accounts = self.orgs.project_accounts(digitalocean=doc)
        a = self.orgs.Assigner()
        shop = {"id": "project:shop", "owners": ["person-org"], **{
            f"organization{k}": v for k, v in (("", "person"), ("_source", "default"), ("_why", "no rule matched"))}}
        declared = {"id": "project:both", "owners": [], "organization": "person",
                    "organization_source": "declared", "organization_why": "the operator"}
        changed = self.orgs.reassign(a, [shop, declared], accounts)
        self.assertEqual(changed, ["project:shop"])
        self.assertEqual((shop["organization"], shop["organization_source"]), ("company", "rule"))
        self.assertEqual(declared["organization"], "person")
        self.assertEqual(self.orgs.reassign(a, [shop, declared], accounts), [], "a second pass changes nothing")

    def test_the_emit_writes_account_organizations_and_assigns_through_them(self):
        import subprocess
        sys.path.insert(0, str(ROOT / "tests"))
        from emitter_fixture import seed
        root = Path(tempfile.mkdtemp(dir=self.tmp.name)).resolve()
        env = seed(root)
        self.write("products.json", {"products": {}, "roles": []})
        self.write("organizations.json", {"organizations": {
            "company": {"label": "Company", "match": {"digitalocean_teams": ["Company Team"]}},
            "person": {"label": "Person", "default": True}}})
        (root / "raw" / "digitalocean.json").write_text(json.dumps({"scanned_on": "2026-11-08", "degraded": [], "teams": [
            {"label": "company-team", "team_uuid": TEAM, "team_name": "Company Team", "token_kind": "stash",
             "apps": [{"id": "a1", "name": "service-api", "serving": True, "repositories": ["fixture/service"]}]},
            {"label": "other-team", "team_uuid": SHARED_TEAM, "team_name": "Other Team", "token_kind": "stash",
             "apps": []}]}), encoding="utf-8")
        p = subprocess.run([sys.executable, "collectors/emit_registry.py", str(root / "raw")], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-600:])
        self.assertIn("1 project(s) assigned through their provider accounts", p.stdout)
        projects = {x["id"]: x for x in json.loads((root / "registry" / "projects.json").read_text())["projects"]}
        a = projects["project:fixture-a"]
        self.assertEqual((a["organization"], a["organization_source"]), ("company", "rule"))
        self.assertIn("DigitalOcean team Company Team (app service-api)", a["organization_why"])
        self.assertEqual(projects["project:fixture-b"]["organization_source"], "external",
                         "a project with no resource in a named account keeps the answer it had")
        accounts = {x["team_uuid"]: x for x in
                    json.loads((root / "registry" / "digitalocean-account.json").read_text())["accounts"]}
        self.assertEqual(accounts[TEAM]["organization"], "company")
        self.assertEqual((accounts[SHARED_TEAM]["organization"], accounts[SHARED_TEAM]["organization_source"]),
                         (None, "none"))

    def test_rows_name_their_account_row(self):
        doc = do_doc(self.infra)
        ids = {a["id"] for a in doc["accounts"]}
        self.assertTrue(all(r["account_ref"] in ids for r in doc["resources"]))
        self.assertEqual({a["team_uuid"] for a in doc["accounts"]}, {TEAM, SHARED_TEAM})


if __name__ == "__main__":
    unittest.main()
