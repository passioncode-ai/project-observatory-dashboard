#!/usr/bin/env python3
"""PROJECT is the project's folder name, and its registry id names the same folder.

The defect this suite pins: the vault keyed slots by FOLDER (`alpha-web`) while
the registry, the MCP answers and the dashboard name the project by id
(`project:local-alpha-web`), and nothing said which a person types. So both
were typed: `vault.py put local-alpha-web …` made a second directory, the Keys
page called that slot an organisation's credential, the MCP `use` command named
one folder while listing slots from two, and the keyserver refused the slug the
vault had accepted.

Every door now resolves a project string the same way (`vault_project.py`):
folder, registry id or name, normalised to the project's ONE folder — and a
string two projects claim is refused rather than guessed. The fixture plants
exactly that trap: a folder literally named `local-zeta` that belongs to
another project than the one whose id is `project:local-zeta`.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = pathlib.Path(tempfile.mkdtemp(prefix="observatory-vault-project-")).resolve()
REGISTRY = BASE / "registry"
STORE = BASE / "secrets" / "projects"
SCRATCH = BASE / "raw"
DATA = BASE / "projects"
STATE = BASE / "state"
ENV = {"OBSERVATORY_REGISTRY": str(REGISTRY), "OBSERVATORY_VAULT_DIR": str(STORE),
       "OBSERVATORY_SCRATCH": str(SCRATCH), "OBSERVATORY_STATE": str(STATE)}
os.environ.update(ENV)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "collectors"))
import paths  # noqa: E402
import vault_project  # noqa: E402

PROJECTS = [
    {"id": "project:local-alpha-web", "name": "alpha-web", "local_folders": ["alpha-web"]},
    {"id": "project:local-beta-api", "name": "beta-api", "local_folders": ["beta-api"]},
    {"id": "project:gamma", "name": "gamma", "local_folders": ["gamma-one", "gamma-two"]},
    {"id": "project:local-zeta", "name": "zeta", "local_folders": ["zeta"]},
    {"id": "project:eta", "name": "eta", "local_folders": ["local-zeta"]},
    {"id": "project:remote-only", "name": "remote-only", "local_folders": []},
]


def plant_registry() -> None:
    REGISTRY.mkdir(parents=True, exist_ok=True)
    (REGISTRY / "projects.json").write_text(json.dumps({"projects": [
        {**p, "ownership": "owned", "owners": [], "lifecycle": "active",
         "membership_rules": []} for p in PROJECTS]}))
    (REGISTRY / "repositories.json").write_text(json.dumps({"repositories": []}))
    (REGISTRY / "relations.json").write_text(json.dumps({"relations": []}))


def run(tool: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "tools" / tool), *args], input=stdin,
                          env={**os.environ, **ENV}, capture_output=True, text=True, timeout=120)


def plant_slot(folder: str, env: str, name: str, value: str = "synthetic-slot-value") -> None:
    d = STORE / folder / env
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / name).write_text(value)
    (d / name).chmod(0o600)


class ResolverTests(unittest.TestCase):
    def test_folder_id_slug_and_name_name_one_folder(self):
        for text in ("alpha-web", "local-alpha-web", "project:local-alpha-web"):
            r = vault_project.resolve(text, PROJECTS)
            self.assertEqual((r.folder, r.project_id), ("alpha-web", "project:local-alpha-web"), text)
        self.assertFalse(vault_project.resolve("alpha-web", PROJECTS).changed)
        self.assertTrue(vault_project.resolve("local-alpha-web", PROJECTS).changed)

    def test_a_string_two_projects_claim_is_ambiguous_never_guessed(self):
        r = vault_project.resolve("local-zeta", PROJECTS)
        self.assertEqual(r.how, "ambiguous")
        self.assertIsNone(r.folder)
        said = r.sentence()
        self.assertIn("project:eta", said)
        self.assertIn("project:local-zeta", said)
        r = vault_project.resolve("project:gamma", PROJECTS)
        self.assertEqual(r.how, "ambiguous", "two folders of different names, neither named")
        self.assertEqual(vault_project.resolve("gamma-two", PROJECTS).folder, "gamma-two")

    def test_unknown_is_kept_and_a_folderless_project_uses_its_slug(self):
        r = vault_project.resolve("acme-ads", PROJECTS)
        self.assertEqual((r.how, r.folder, r.project_id), ("unknown", "acme-ads", None))
        r = vault_project.resolve("project:remote-only", PROJECTS)
        self.assertEqual((r.folder, r.project_id), ("remote-only", "project:remote-only"))
        r = vault_project.resolve("alpha-web", None, loaded=True)
        self.assertEqual((r.how, r.folder), ("unreadable", "alpha-web"))


class DoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        plant_registry()

    def test_vault_files_a_registry_id_under_the_projects_folder_and_says_so(self):
        p = run("vault.py", "put", "local-alpha-web", "local", "SLUG_KEY", stdin="synthetic-one")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((STORE / "alpha-web/local/SLUG_KEY").is_file())
        self.assertFalse((STORE / "local-alpha-web").exists(), "a second directory for one project")
        self.assertIn("'alpha-web'", p.stdout, "the folder used is printed")
        p = run("vault.py", "put", "project:local-alpha-web", "local", "ID_KEY", stdin="synthetic-two")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((STORE / "alpha-web/local/ID_KEY").is_file())
        p = run("vault.py", "leak", "project:local-alpha-web", "local", "ID_KEY",
                "--where", "a fixture log line, job 7")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("recorded: leak:", p.stdout)
        self.assertIn("alpha-web/local/ID_KEY", p.stdout)

    def test_vault_refuses_an_ambiguous_name_and_writes_nothing(self):
        p = run("vault.py", "put", "local-zeta", "prod", "AMBIGUOUS_KEY", stdin="synthetic-three")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("project:eta", p.stderr)
        self.assertIn("project:local-zeta", p.stderr)
        self.assertFalse(list(STORE.rglob("AMBIGUOUS_KEY")))

    def test_an_unknown_name_is_an_organisations_folder_and_a_legacy_slot_stays_reachable(self):
        p = run("vault.py", "put", "acme-ads", "prod", "ADS_TOKEN", stdin="synthetic-four")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((STORE / "acme-ads/prod/ADS_TOKEN").is_file())
        plant_slot("local-beta-api", "prod", "LEGACY_KEY")
        p = run("vault.py", "rotate", "local-beta-api", "prod", "LEGACY_KEY", stdin="synthetic-five")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("'local-beta-api'", p.stdout)
        p = run("vault.py", "put", "project:not-registered", "prod", "X_KEY", stdin="synthetic-six")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("folder name", p.stderr)

    def test_use_secret_accepts_the_registry_id(self):
        plant_slot("remote-only", "stage", "ONLY_STAGE_TOKEN")
        p = run("use_secret.py", "names", "project:remote-only")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ONLY_STAGE_TOKEN", p.stdout)
        p = run("use_secret.py", "run", "--env", "prod", "project:remote-only", "ONLY_STAGE_TOKEN",
                "--", sys.executable, "-c", "print('ran')")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("ran", p.stdout)
        self.assertIn("{prod}", p.stderr, "the message names the environment actually searched")
        self.assertIn("remote-only/stage", p.stderr, "and where the name does live")
        self.assertIn("--env stage", p.stderr)
        self.assertIn('full-path)/tools/vault.py" put', p.stderr)

    def test_names_lists_every_folder_of_one_project(self):
        # 2026-10-06: a key issued into the project's folder (`beta-api`) was absent
        # from `names` for the registry slug whose folder held older slots.
        plant_slot("local-beta-api", "prod", "OLDER_KEY")
        plant_slot("beta-api", "stage", "NEWER_KEY")
        plant_slot("alpha-web", "prod", "ANOTHER_PROJECTS_KEY")
        for typed in ("local-beta-api", "beta-api", "project:local-beta-api"):
            with self.subTest(typed=typed):
                p = run("use_secret.py", "names", typed)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("vault:local-beta-api/prod", p.stdout)
                self.assertIn("vault:beta-api/stage", p.stdout)
                self.assertNotIn("ANOTHER_PROJECTS_KEY", p.stdout)

    def test_a_key_put_by_one_name_is_read_by_the_other(self):
        # 2026-10-06: `put` filed a key under the project's folder while the older slots
        # sat under the registry name; `run` and `list` by that name did not find it.
        plant_slot("local-beta-api", "prod", "OLDER_KEY")
        p = run("vault.py", "put", "local-beta-api", "prod", "NEWER_KEY", stdin="synthetic-newer")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((STORE / "beta-api/prod/NEWER_KEY").is_file(), "put files under the project's folder")
        p = run("use_secret.py", "run", "--env", "prod", "local-beta-api", "NEWER_KEY",
                "--", sys.executable, "-c", "import os; print(len(os.environ['NEWER_KEY']))")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), str(len("synthetic-newer")))
        p = run("vault.py", "list", "local-beta-api", "prod")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("local-beta-api/prod/OLDER_KEY", p.stdout)
        self.assertIn("beta-api/prod/NEWER_KEY", p.stdout)

    def test_keyserver_mint_destination_uses_the_same_rule(self):
        import keyserver
        self.assertEqual(keyserver.check_destination("vault:local-beta-api/prod/OPENROUTER_API_KEY"),
                         "vault:beta-api/prod/OPENROUTER_API_KEY")
        self.assertEqual(keyserver.check_destination("vault:beta-api/prod/OPENROUTER_API_KEY"),
                         "vault:beta-api/prod/OPENROUTER_API_KEY")
        with self.assertRaises(ValueError) as caught:
            keyserver.check_destination("vault:local-zeta/prod/OPENROUTER_API_KEY")
        self.assertIn("project:eta", str(caught.exception))
        with self.assertRaises(ValueError):
            keyserver.check_destination("vault:nosuch-project/prod/OPENROUTER_API_KEY")

    def test_the_registry_join_and_the_mcp_answer_agree(self):
        plant_slot("beta-api", "prod", "CF_API_TOKEN")
        plant_slot("local-beta-api", "local", "OLD_SLUG_KEY")
        plant_slot("local-zeta", "prod", "WHOSE_KEY")
        import credentials_registry
        with patch.object(credentials_registry, "from_machine_secrets", lambda: []), \
                patch.object(credentials_registry, "from_project_secrets", lambda: []):
            creds, edges = credentials_registry.records({}, STORE, PROJECTS)
        owner = {e["from"]: e["to"] for e in edges}
        self.assertEqual(owner.get("credential:vault/local-beta-api/local/OLD_SLUG_KEY"),
                         "project:local-beta-api", "a slug-named folder belongs to its project")
        whose = next(c for c in creds if c["id"] == "credential:vault/local-zeta/prod/WHOSE_KEY")
        self.assertNotIn("credential:vault/local-zeta/prod/WHOSE_KEY", owner)
        self.assertTrue(whose.get("unclaimed_reason", "").startswith("ambiguous"), whose)
        ads = next(c for c in creds if c["id"] == "credential:vault/acme-ads/prod/ADS_TOKEN"
                   ) if (STORE / "acme-ads").exists() else None
        if ads is not None:
            self.assertIn("organisation", ads.get("unclaimed_reason", ""))

        import survey
        out = survey.credentials("project:local-beta-api")
        rows = {r["name"]: r for r in out["vault"]}
        self.assertEqual(rows["CF_API_TOKEN"]["project"], "beta-api")
        self.assertEqual(rows["OLD_SLUG_KEY"]["project"], "local-beta-api")
        self.assertIn(" local-beta-api OLD_SLUG_KEY ", rows["OLD_SLUG_KEY"]["use"])
        self.assertIn("--env prod beta-api CF_API_TOKEN ", rows["CF_API_TOKEN"]["use"])
        self.assertEqual(out["project"], "beta-api", "a new slot goes under the project's folder")
        self.assertEqual(survey.credentials("beta-api")["projectId"], "project:local-beta-api")


class FreshWorkspaceVault(unittest.TestCase):
    """Audit A10: a workspace's own vault does not exist until the first `vault.py put`."""

    def test_the_absent_default_vault_is_empty_not_unreadable(self):
        import importlib
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp).resolve() / "home"
            env = {k: v for k, v in os.environ.items() if k != "OBSERVATORY_VAULT_DIR"}
            env["OBSERVATORY_HOME"] = str(home)
            with patch.dict(os.environ, env, clear=True):
                import workspace
                workspace.initialize(home)
                import paths
                importlib.reload(paths)
                import survey
                importlib.reload(survey)
                self.assertFalse((paths.SECRETS / "projects").exists())
                out = survey.credentials("project:fresh-one")
                self.assertFalse([d for d in out["degraded"] if d["source"] == "vault"], out["degraded"])
                with patch.dict(os.environ, {"OBSERVATORY_VAULT_DIR": str(pathlib.Path(tmp) / "elsewhere")}):
                    out = survey.credentials("project:fresh-one")
                self.assertTrue([d for d in out["degraded"] if d["source"] == "vault"],
                                "a vault named elsewhere that is not there is still unknown")
            importlib.reload(paths)
            importlib.reload(survey)


if __name__ == "__main__":
    unittest.main()
