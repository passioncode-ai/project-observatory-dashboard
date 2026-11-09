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
        root = Path(self.tmp.name).resolve() / "estate"
        root.mkdir()
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

    def test_a_second_emit_with_nothing_new_carries_the_projects_date(self):
        """Review of #479, finding 1: projects.json was written twice per emit, first without the
        account-derived organizations, so its `updated_on` moved every day with the same content."""
        import subprocess
        sys.path.insert(0, str(ROOT / "tests"))
        from emitter_fixture import seed
        root = Path(self.tmp.name).resolve() / "estate"
        root.mkdir()
        env = seed(root)
        self.write("products.json", {"products": {}, "roles": []})
        self.write("organizations.json", {"organizations": {
            "company": {"label": "Company", "match": {"digitalocean_teams": [TEAM]}},
            "person": {"label": "Person", "default": True}}})
        (root / "raw" / "digitalocean.json").write_text(json.dumps({"scanned_on": "2026-11-08", "degraded": [], "teams": [
            {"label": "company-team", "team_uuid": TEAM, "team_name": "Company Team", "token_kind": "stash",
             "apps": [{"id": "a1", "name": "service-api", "serving": True, "repositories": ["fixture/service"]}]}]}),
            encoding="utf-8")
        reg = root / "registry" / "projects.json"

        def emit():
            p = subprocess.run([sys.executable, "collectors/emit_registry.py", str(root / "raw")], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-600:])
            return json.loads(reg.read_text(encoding="utf-8"))

        first = emit()
        self.assertEqual({x["id"]: x["organization"] for x in first["projects"]}["project:fixture-a"], "company")
        first["updated_on"] = "2026-01-01"                    # as an earlier day's emit wrote it
        reg.write_text(json.dumps(first), encoding="utf-8")
        second = emit()
        self.assertEqual(second["updated_on"], "2026-01-01", "nothing changed, so the date is carried")
        self.assertEqual(second["projects"], first["projects"])

    def test_a_string_where_a_list_belongs_matches_nothing_and_is_named(self):
        orgs = json.loads(json.dumps(self.config))
        orgs["organizations"]["company"]["match"] = {"heroku_teams": "c", "repository_owners": "company-org"}
        a = self.orgs.Assigner(doc=orgs, estate_owners={"company-org"}, products={})
        self.assertEqual(a.account("heroku", ["c"])["organization"], None, "a string is not read letter by letter")
        self.assertEqual(a.assign({"id": "project:x", "owners": ["company-org"]})["source"], "default")
        self.assertEqual(sorted(a.problems), ["company: match.heroku_teams is not a known list rule; ignored",
                                              "company: match.repository_owners is not a known list rule; ignored"])

    def test_a_team_name_two_teams_bear_decides_nothing_and_is_warned_about(self):
        orgs = json.loads(json.dumps(self.config))
        orgs["organizations"]["company"]["match"] = {"digitalocean_teams": ["Twin"]}
        orgs["organizations"]["person"]["match"] = {"digitalocean_teams": [SHARED_TEAM]}
        a = self.orgs.Assigner(doc=orgs)
        accounts = [{"id": "digitalocean:team:" + TEAM, "team_uuid": TEAM, "name": "Twin"},
                    {"id": "digitalocean:team:" + SHARED_TEAM, "team_uuid": SHARED_TEAM, "name": "twin"}]
        warnings = self.orgs.annotate_accounts(a, "digitalocean", accounts)
        self.assertEqual(len(warnings), 1)
        self.assertIn("'Twin', the name of 2 teams here", warnings[0])
        self.assertEqual([x["organization"] for x in accounts], [None, "person"], "the uuid still decides")
        self.assertEqual(self.orgs.account_keys("digitalocean", accounts)["digitalocean:team:" + TEAM], [TEAM])

    def test_rows_name_their_account_row(self):
        doc = do_doc(self.infra)
        ids = {a["id"] for a in doc["accounts"]}
        self.assertTrue(all(r["account_ref"] in ids for r in doc["resources"]))
        self.assertEqual({a["team_uuid"] for a in doc["accounts"]}, {TEAM, SHARED_TEAM})



class RulesPart2(Base):
    """OBS-412 part 2: `github_owners`, `openrouter_accounts`, one reading for every reader (ORG-R26, ORG-R28)."""

    def test_github_owners_is_repository_owners_by_its_onboarding_name(self):
        orgs = json.loads(json.dumps(ORGS))
        orgs["organizations"]["company"]["match"] = {"github_owners": ["company-org"],
                                                     "repository_owners": ["company-org", "company-bb"]}
        a = self.orgs.Assigner(doc=orgs)
        got = a.assign({"id": "project:x", "owners": ["company-org"]})
        self.assertEqual((got["organization"], got["source"]), ("company", "rule"))
        self.assertEqual(got["why"], "repository owner company-org", "an owner named by both rules counts once")
        self.assertEqual(a.account("owner", ["COMPANY-ORG"])["organization"], "company")
        self.assertIn("match.repository_owners and match.github_owners", a.account("owner", ["company-org"])["organization_why"])
        self.assertEqual(a.account("owner", ["nobody"])["organization_source"], "none")
        self.assertEqual(self.orgs.validate(orgs), [])

    def test_an_unknown_rule_is_refused_with_the_known_ones_named(self):
        bad = json.loads(json.dumps(ORGS))
        bad["organizations"]["company"]["match"]["gitlab_groups"] = ["g"]
        bad["organizations"]["company"]["match"]["openrouter_accounts"] = "main"
        problems = self.orgs.validate(bad)
        unknown = next(p for p in problems if "gitlab_groups" in p)
        self.assertIn("match.gitlab_groups is not a known list rule", unknown)
        for rule in ("github_owners", "openrouter_accounts", "digitalocean_teams"):
            self.assertIn(rule, unknown)
        self.assertTrue(any("match.openrouter_accounts" in p and "list of strings" in p for p in problems))
        a = self.orgs.Assigner(doc=bad)
        self.assertTrue(any("gitlab_groups" in p and "known:" in p for p in a.problems))

    def test_an_openrouter_account_has_an_organization_and_decides_no_project(self):
        orgs = json.loads(json.dumps(ORGS))
        orgs["organizations"]["company"]["match"]["openrouter_accounts"] = ["main"]
        a = self.orgs.Assigner(doc=orgs)
        self.assertEqual(a.account("openrouter", ["main"])["organization"], "company")
        got = a.assign({"id": "project:mine", "owners": ["person-org"]},
                       [{"provider": "openrouter", "keys": ["main"], "label": "main", "via": "key k"}])
        self.assertEqual(got["source"], "default", "a key's issuer is weighed per key (ORG-R06), never per project")

    def test_one_reading_for_every_reader(self):
        r = self.orgs.reading
        self.assertEqual(r({"id": "p"}), {"state": "unset"})
        self.assertEqual(r({"organization": "company", "organization_source": "rule"})["destinations"]["ga4Account"],
                         "accounts/100")
        self.assertEqual(r({"organization": "external", "organization_source": "external"})["state"], "external")
        self.assertEqual(r({"organization": "external", "organization_source": "declared"})["state"], "external")
        self.assertEqual(r({"organization": None, "organization_source": "conflict"})["state"], "unresolved")
        proposed = r({"organization": "company", "organization_source": "proposed"})
        self.assertEqual(proposed["state"], "unresolved", "a source this engine does not know is no decision")
        self.assertNotIn("destinations", proposed)

    def test_a_proposed_project_gets_no_account_in_the_session_line_the_view_or_the_findings(self):
        import session_start
        importlib.reload(session_start)
        sys.path.insert(0, str(ROOT / "dashboard"))
        import i18n
        project = {"id": "project:co", "name": "co", "ownership": "owned", "lifecycle": "active",
                   "organization": "company", "organization_source": "proposed", "organization_why": "rule"}
        line = session_start.organization_line(project, i18n.Translator("en"))
        self.assertIn("not decided (proposed: company)", line)
        self.assertNotIn("accounts/100", line)
        self.assertIn("организация не решена", session_start.organization_line(project, i18n.Translator("ru")))
        import survey
        importlib.reload(survey)
        view = survey._project_view(project, {}, {})
        self.assertEqual(view["organization"]["state"], "unresolved")
        self.assertNotIn("ga4Account", view["organization"])
        import google_findings
        importlib.reload(google_findings)
        doc = {"properties": [{"property": "properties/9", "project": "project:co", "account": "accounts/200"}],
               "accounts": [{"account": "accounts/100"}, {"account": "accounts/200"}]}
        self.assertEqual([f for f in google_findings.organization_findings(doc, [project], ORGS)
                          if f["type"] == "analytics.property_wrong_account"], [])

    def test_a_conflict_and_an_unread_rule_are_named_on_the_board(self):
        import organization_findings
        importlib.reload(organization_findings)
        projects = [{"id": "project:both", "name": "both", "organization": None, "organization_source": "conflict",
                     "organization_why": "company (repository owner a) vs person (repository owner b)"},
                    {"id": "project:ok", "organization": "company", "organization_source": "rule"}]
        out = organization_findings.findings(projects, ORGS)
        self.assertEqual([(f["type"], f["subject"]) for f in out], [("organization.unresolved", "project:both")])
        self.assertEqual(out[0]["title"], "both has no organization: its rules disagree")
        self.assertIn("company (repository owner a) vs person", out[0]["detail"])
        bad = json.loads(json.dumps(ORGS))
        bad["organizations"]["company"]["match"]["aws_accounts"] = ["1"]
        out = organization_findings.findings([], bad)
        self.assertEqual([f["type"] for f in out], ["organization.config_problem"])
        self.assertIn("match.aws_accounts is not a known list rule", out[0]["detail"])
        self.assertEqual(organization_findings.findings(projects, {}), [], "silent when nothing is configured")
        (self.home / "config" / "organizations.json").write_text("{not json")
        self.assertEqual(self.orgs.load_problem(), "not JSON (line 1)")
        out = organization_findings.findings(projects, {}, self.orgs.load_problem())
        self.assertEqual([f["title"] for f in out], ["organizations.json is not read: not JSON (line 1)"])


OR_TOKEN = "synthetic-not-a-value"


class Views(Base):
    """OBS-411 and OBS-415: `full org accounts` and `full org keys`, read-only and without a value."""

    def setUp(self):
        super().setUp()
        import paths
        self.secrets = Path(paths.source_path("secret_store", paths.SECRETS))
        self.doctl = Path(self.tmp.name).resolve() / "doctl.yaml"
        os.environ["DOCTL_CONFIG"] = str(self.doctl)
        orgs = json.loads(json.dumps(ORGS))
        orgs["organizations"]["company"]["match"].update(
            digitalocean_teams=[TEAM, SHARED_TEAM], openrouter_accounts=["company-or"],
            cloudflare_accounts=["acc-co"], heroku_teams=["co-apps"])
        orgs["organizations"]["person"]["match"] = {"digitalocean_teams": [SHARED_TEAM],
                                                    "openrouter_accounts": ["main"]}
        self.write("organizations.json", orgs)
        self.registry("projects.json", {"projects": [
            {"id": "project:co-web", "name": "co-web", "owners": ["company-org"], "local_folders": ["co-web"],
             "organization": "company", "organization_source": "rule", "organization_why": "repository owner company-org"},
            {"id": "project:mine", "name": "mine", "owners": ["person-org"], "local_folders": ["mine"],
             "organization": "person", "organization_source": "default", "organization_why": "no rule matched"},
            {"id": "project:both", "name": "both", "owners": ["person-org"], "local_folders": ["both"],
             "organization": None, "organization_source": "conflict",
             "organization_why": "company (DigitalOcean team Shared) vs person (DigitalOcean team Shared)"},
            {"id": "project:clone", "name": "clone", "owners": ["stranger"], "local_folders": ["clone"],
             "organization": "external", "organization_source": "external", "organization_why": "outside"}]})
        self.registry("repositories.json", {"repositories": [
            {"name_with_owner": "company-org/co-web", "host": "github"},
            {"name_with_owner": "company-bb/tool", "host": "bitbucket"}]})
        self.registry("digitalocean-account.json", {"scanned_on": "2026-11-08", "degraded": [
            {"source": "digitalocean:doctl:default", "reason": "member of team X via doctl context default"}], "accounts": [
            {"id": f"digitalocean:team:{SHARED_TEAM}", "team_uuid": SHARED_TEAM, "name": "Shared"}], "resources": [
            {"id": "digitalocean:app:a3", "kind": "app", "name": "both-api", "project": "project:both",
             "link_rule": "digitalocean-app-repo", "account_ref": f"digitalocean:team:{SHARED_TEAM}"}]})
        gh = paths.SCRATCH / "gh"
        gh.mkdir(parents=True, exist_ok=True)
        for owner in ("company-org", "person-org"):
            (gh / f"{owner}.json").write_text("[]")
        # A door's stash and records: value files hold a planted value nothing may print, and are made
        # unreadable to prove no value file is opened (ORG-R03, the rows' "never a value").
        self.private("openrouter-admin/company-or", OR_TOKEN)
        self.private("openrouter-admin/company-or.meta.json", json.dumps({"kind": "provisioning"}))
        self.private("digitalocean-admin/company-team", OR_TOKEN)
        self.private("digitalocean-admin/company-team.meta.json",
                     json.dumps({"kind": "team", "team_uuid": TEAM, "team_name": "Company Team"}))
        self.private("openrouter-issued.json", json.dumps({"issued": {
            "co-web-llm": {"account": "company-or", "destination": "vault:co-web/prod/OPENROUTER_API_KEY"},
            "mine-llm": {"account": "main", "destination": "vault:mine/prod/OPENROUTER_API_KEY"},
            "co-from-main": {"account": "main", "destination": "vault:co-web/stage/OPENROUTER_API_KEY"},
            "both-llm": {"account": "main", "destination": "vault:both/prod/OPENROUTER_API_KEY"},
            "mine-from-co": {"account": "company-or", "destination": "vault:mine/local/OPENROUTER_API_KEY"}}}))
        self.private("heroku-issued.json", json.dumps({"issued": {
            "co-web/prod/EXAMPLE_HEROKU_TOKEN": {"team": "co-apps"}}}))
        for slot in ("co-web/prod/OPENROUTER_API_KEY", "co-web/stage/OPENROUTER_API_KEY", "co-web/prod/EXAMPLE_HEROKU_TOKEN",
                     "co-web/prod/HAND_PUT", "co-web/qa/OPENROUTER_API_KEY", "co-web/prod/CF_TOKEN",
                     "mine/prod/OPENROUTER_API_KEY", "mine/local/OPENROUTER_API_KEY", "both/prod/OPENROUTER_API_KEY"):
            self.private(f"projects/{slot}", OR_TOKEN)
        for slot in ("co-web/prod/BLOB_ACCESS_ID", "co-web/prod/BLOB_SECRET"):
            self.private(f"projects/{slot}", OR_TOKEN)
        self.private("projects/movements.jsonl", "".join(json.dumps(row) + "\n" for row in (
            {"event": "issue", "secret": "co-web/prod/CF_TOKEN", "tool": "cloudflare.py", "account": "Co Account"},
            {"event": "issue", "secret": "co-web/prod/BLOB_*", "tool": "cloudflare.py", "account": "Co Account"})))
        self.private("digitalocean-issued/other.meta.json", json.dumps(
            {"label": "other", "team_uuid": "33333333-3333-4333-8333-333333333333", "team_name": "Elsewhere",
             "vault": "mine/prod/DO_TOKEN"}))
        self.private("projects/mine/prod/DO_TOKEN", OR_TOKEN)
        self.registry("cloudflare-account.json", {"accounts": [
            {"id": "cloudflare:account:acc-co", "account_id": "acc-co", "name": "Co Account"},
            {"id": "cloudflare:account:acc-loose", "account_id": "acc-loose", "name": "Loose"}], "resources": []})
        self.doctl.write_text("access-token: " + OR_TOKEN + "-doctl\n")
        self.doctl.chmod(0o600)
        import org_view
        self.view = importlib.reload(org_view)
        sys.path.insert(0, str(ROOT / "tools"))
        for name in ("vault", "digitalocean"):
            importlib.reload(importlib.import_module(name))

    def tearDown(self):
        for path in self.secrets.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        super().tearDown()

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
        """Every value file unreadable to its own owner: a view that opened one would fail on it."""
        for path in self.secrets.rglob("*"):
            if path.is_file() and not path.name.endswith((".json", ".jsonl")):
                path.chmod(0)

    def test_every_account_once_under_one_organization_or_a_decision(self):
        v = self.view.accounts()
        self.assertEqual(v["schema"], "org-accounts/1")
        rows = {r["id"]: r for r in v["accounts"]}
        placed = [i for g in v["organizations"] for i in g["accounts"]] + v["needsDecision"]
        self.assertEqual(sorted(placed), sorted(rows), "every account exactly once")
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual((rows["github:company-org"]["organization"], rows["github:company-org"]["source"]),
                         ("company", "rule"))
        self.assertEqual((rows["github:person-org"]["organization"], rows["github:person-org"]["source"]),
                         ("person", "default"))
        self.assertEqual(rows["github:person-org"]["holds"]["projects"], ["project:both", "project:mine"])
        self.assertEqual((rows["github:stranger"]["organization"], rows["github:stranger"]["source"]),
                         ("external", "external"))
        self.assertFalse(rows["github:stranger"]["connected"])
        self.assertEqual(rows["bitbucket:company-bb"]["provider"], "bitbucket")
        shared = rows[f"digitalocean:team:{SHARED_TEAM}"]
        self.assertEqual((shared["organization"], shared["source"], shared["claimedBy"]),
                         (None, "conflict", ["company", "person"]))
        self.assertIn(shared["id"], v["needsDecision"])
        self.assertEqual(shared["holds"]["projects"], ["project:both"])
        team = rows[f"digitalocean:team:{TEAM}"]
        self.assertEqual((team["organization"], team["connected"]), ("company", True))
        self.assertEqual(rows["openrouter:account:company-or"]["organization"], "company")
        self.assertEqual(rows["openrouter:account:company-or"]["holds"]["keys"], 2)
        main = rows["openrouter:account:main"]
        self.assertEqual((main["organization"], main["connected"]), ("person", False))
        self.assertIn("stash --as main", main["decision"]["command"])
        self.assertEqual(rows["heroku:team:co-apps"]["holds"]["keys"], 1)
        self.assertFalse(rows["heroku:team:co-apps"]["connected"], "known from a ledger row alone")
        self.assertIn("heroku:team:co-apps", v["needsDecision"])
        self.assertEqual(rows["cloudflare:account:acc-co"]["holds"]["keys"], 3,
                         "the Cloudflare door's issuer is read from the movements journal, an R2 pair per slot")
        loose = rows["cloudflare:account:acc-loose"]
        self.assertEqual((loose["organization"], loose["source"]), (None, "none"),
                         "a provider account no rule names is undecided, as its registry row says (ORG-R26)")
        self.assertIn(loose["id"], v["needsDecision"])
        elsewhere = rows["digitalocean:team:33333333-3333-4333-8333-333333333333"]
        self.assertEqual((elsewhere["connected"], elsewhere["holds"]["keys"]), (False, 1))
        self.assertIn("digitalocean.py\" stash", elsewhere["decision"]["command"])
        self.assertFalse([d for d in v["degraded"] if "doctl" in d["source"]],
                         "a doctl context the scan named is a decision here, not a failed read")
        doctl = rows["digitalocean:doctl:default"]
        self.assertIn(doctl["id"], v["needsDecision"])
        self.assertIn("stash --from-doctl default", doctl["decision"]["command"])
        groups = {g["name"]: g for g in v["organizations"]}
        self.assertEqual([g["name"] for g in v["organizations"]], ["person", "company", "external"])
        self.assertEqual((groups["person"]["label"], groups["person"]["kind"]), ("Example Person", "personal"))
        self.assertEqual(groups["company"]["projects"], ["project:co-web"])
        self.assertNotIn(OR_TOKEN, json.dumps(v))
        self.assertEqual(v["state"], "complete", v["degraded"])

    def test_the_accounts_text_and_the_cli_say_the_same(self):
        import org as org_cli
        importlib.reload(org_cli)
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(org_cli.main(["accounts", "--json"]), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["needsDecision"], self.view.accounts()["needsDecision"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(org_cli.main(["accounts"]), 0)
        text = out.getvalue()
        self.assertIn("Example Company (local)", text)
        self.assertIn("Needs a decision (", text)
        self.assertIn("--from-doctl default", text)
        self.assertNotIn(OR_TOKEN, text)

    def test_empty_unconfigured_and_degraded_are_said(self):
        import paths
        for name in ("projects.json", "repositories.json", "digitalocean-account.json", "cloudflare-account.json"):
            (paths.REGISTRY / name).unlink()
        v = self.view.accounts()
        sources = {d["source"]: d["code"] for d in v["degraded"]}
        self.assertEqual(sources["registry/projects.json"], "missing")
        self.assertEqual(sources["digitalocean"], "not-scanned", "a stashed team with no scan is named")
        self.assertEqual(v["state"], "partial")
        self.write("organizations.json", {"organizations": {}, "projects": {}})
        v = self.view.accounts()
        self.assertFalse(v["configured"])
        self.assertEqual(v["default"], "personal")
        self.assertEqual(v["organizations"][0], {**v["organizations"][0], "name": "personal", "label": "Personal",
                                                 "implicit": True})
        self.assertTrue(all(r["source"] in ("default", "external") for r in v["accounts"]))
        import shutil
        shutil.rmtree(self.secrets)
        shutil.rmtree(paths.SCRATCH / "gh")
        self.doctl.unlink()
        for name in os.listdir(paths.REGISTRY):
            (paths.REGISTRY / name).unlink()
        v = self.view.accounts()
        self.assertEqual((v["state"], v["accounts"], v["needsDecision"]), ("empty", [], []))
        self.assertIn("No accounts connected yet", self.view.accounts_text(v))

    def test_keys_classified_by_project_and_issuer_without_opening_a_value(self):
        self.seal_values()
        v = self.view.keys("company")
        self.assertEqual(v["schema"], "org-keys/1")
        self.assertEqual([d for d in v["degraded"] if "vault" in d["source"] or "stash" in d["source"]], [])
        k = {r["slot"]: r for r in v["keys"]}
        own = k["co-web/prod/OPENROUTER_API_KEY"]
        self.assertEqual((own["class"], own["selected"], own["proposal"]), ("organization", True, "hand-over"))
        self.assertEqual(own["issuer"]["account"], "company-or")
        self.assertEqual(k["co-web/stage/OPENROUTER_API_KEY"]["class"], "ambiguous",
                         "the organization's project, a personal account's key (ORG-R06)")
        self.assertIn("personal", k["co-web/stage/OPENROUTER_API_KEY"]["why"])
        hand = k["co-web/prod/HAND_PUT"]
        self.assertEqual((hand["class"], hand["putBy"], hand["issuer"]), ("ambiguous", "hand", None))
        self.assertEqual(k["co-web/prod/EXAMPLE_HEROKU_TOKEN"]["class"], "organization")
        self.assertEqual(k["co-web/prod/CF_TOKEN"]["issuer"]["record"], "movements journal (cloudflare.py)")
        self.assertEqual(k["co-web/prod/CF_TOKEN"]["class"], "organization")
        qa = k["co-web/qa/OPENROUTER_API_KEY"]
        self.assertEqual((qa["environmentDeclared"], qa["selected"]), (False, False))
        self.assertEqual(k["mine/local/OPENROUTER_API_KEY"]["class"], "ambiguous",
                         "a personal project's key issued in the organization's account is in scope, undecided")
        self.assertNotIn("mine/prod/OPENROUTER_API_KEY", k, "a personal project's personal key is out of scope")
        self.assertEqual(k["both/prod/OPENROUTER_API_KEY"]["class"], "personal")
        fixed = {(n["provider"], n["label"]): n for n in v["notMovable"]}
        self.assertEqual(fixed[("openrouter", "company-or")]["action"],
                         "revoke after the organization's space stashes its own")
        self.assertEqual(fixed[("digitalocean", "company-team")]["action"],
                         "revoke after the organization's space stashes its own")
        self.assertEqual(k["co-web/prod/BLOB_SECRET"]["class"], "organization",
                         "an R2 pair is journalled once for both slots")
        self.assertNotIn("mine/prod/DO_TOKEN", k, "issued in a team no rule names, in a personal project")
        self.assertEqual(v["counts"], {"organization": 5, "personal": 1, "ambiguous": 4, "notMovable": 2})
        self.assertEqual([p["id"] for p in v["projects"]], ["project:both", "project:co-web"])
        self.assertNotIn(OR_TOKEN, json.dumps(v))
        self.assertIn("ORG-R09", v["notChecked"][0])

    def test_cloudflare_keys_without_a_scan_read_the_admin_tokens_record(self):
        import paths
        (paths.REGISTRY / "cloudflare-account.json").unlink()
        k = {r["slot"]: r for r in self.view.keys("company")["keys"]}
        self.assertEqual(k["co-web/prod/CF_TOKEN"]["issuer"]["source"], "none", "no scan, no stash: no id")
        v = self.view.keys("company")
        self.assertEqual([d["code"] for d in v["degraded"] if d["source"] == "cloudflare:journal"], ["unresolved"])
        self.private("cloudflare-admin/co", OR_TOKEN)
        self.private("cloudflare-admin/co.meta.json", json.dumps(
            {"kind": "admin", "accounts": [{"id": "acc-co", "name": "Co Account"}, {"id": "acc-p", "name": "Mine"}]}))
        v = self.view.keys("company")
        k = {r["slot"]: r for r in v["keys"]}
        self.assertEqual(k["co-web/prod/CF_TOKEN"]["class"], "organization")
        self.assertEqual([d for d in v["degraded"] if d["source"] == "cloudflare:journal"], [])
        cf = next(n for n in v["notMovable"] if n["provider"] == "cloudflare")
        self.assertTrue(cf["action"].startswith("replace:"), "a token over the organization's and other accounts")

    def test_no_default_and_an_unreadable_file_are_said(self):
        orgs = json.loads(json.dumps(ORGS))
        orgs["organizations"]["person"].pop("default")
        self.write("organizations.json", orgs)
        v = self.view.accounts()
        rows = {r["id"]: r for r in v["accounts"]}
        self.assertEqual((rows["github:person-org"]["organization"], rows["github:person-org"]["source"]),
                         (None, "none"))
        self.assertIn("github:person-org", v["needsDecision"])
        self.assertNotIn(None, [g["name"] for g in v["organizations"]])
        self.assertTrue(any("exactly one organization must be `default`" in p for p in v["problems"]))
        (self.home / "config" / "organizations.json").write_text("{broken", encoding="utf-8")
        v = self.view.accounts()
        self.assertIn("config/organizations.json: not JSON (line 1)", v["problems"])
        self.assertIn("is read as if absent", self.view.accounts_text(v))
        with self.assertRaises(self.view.Refusal) as caught:
            self.view.keys("company")
        self.assertEqual(caught.exception.code, "unreadable-organizations")
        self.write("organizations.json", {"organizations": ORGS["organizations"], "projects": ["not", "a", "map"]})
        self.assertIn("projects must be an object keyed by project id", self.orgs.validate(self.orgs.load()))
        self.assertEqual(self.orgs.Assigner().assign({"id": "project:x", "owners": ["company-org"]})["source"], "rule")

    def test_keys_refusals_name_their_command(self):
        for org, code in (("person", "default-organization"), ("nobody", "unknown-organization"),
                          ("sk-or-v1-" + "a" * 40, "invalid-organization")):
            with self.assertRaises(self.view.Refusal) as caught:
                self.view.keys(org)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("a" * 40, str(caught.exception))
        self.write("organizations.json", {"organizations": {}, "projects": {}})
        with self.assertRaises(self.view.Refusal) as caught:
            self.view.keys("company")
        self.assertEqual(caught.exception.answer()["error"], "no-organizations")
        import org as org_cli
        importlib.reload(org_cli)
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(org_cli.main(["keys", "--org", "company", "--json"]), 1)
        self.assertEqual(json.loads(err.getvalue())["error"], "no-organizations")


if __name__ == "__main__":
    unittest.main()
