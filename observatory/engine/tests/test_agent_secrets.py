#!/usr/bin/env python3
"""Every credential an agent uses comes from the vault: the checks that report it.

Each finding is driven from what is on disk — agent memory, the env inventory, the
vault listing, use_secret's journal — and each is watched staying silent where the
rule holds, because a rule that fires on everything is ignored as surely as one
that never fires. Synthetic throughout; no value is printed by any of them.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import agent_secret_findings as F                                                   # noqa: E402
import memory_redact                                                               # noqa: E402
from store import workflow as W                                                    # noqa: E402

VALUE = "a fixture value nobody would guess"


def store_with_workflow(path: pathlib.Path, project: str) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    W.checkpoint_write(conn, owner="agent:fixture", idempotency_key="secrets-0001", step_id="S1",
                       status="done", project_id=f"project:{project}",
                       body={"goal": "g", "credentials": [{"project": project, "env": "prod",
                                                           "name": "API_TOKEN"}]},
                       redactor=memory_redact.Redactor(known_loader=lambda: {}))
    conn.close()


class Findings(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="observatory-agent-secrets-")
        self.root = pathlib.Path(self.dir.name)
        self.db = self.root / "store.db"
        store_with_workflow(self.db, "alpha-agent")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_agent_projects_are_measured_not_guessed(self) -> None:
        folder = self.root / "beta-service"
        folder.mkdir()
        (folder / "fabric-agent.json").write_text("{}", encoding="utf-8")
        plain = self.root / "gamma-site"
        plain.mkdir()
        agents = F.agent_projects(self.db, [{"local_folders": [str(folder)]},
                                            {"local_folders": [str(plain)]}])
        self.assertEqual(set(agents), {"alpha-agent", "beta-service"})

    def test_secrets_outside_the_vault_are_named_with_the_command(self) -> None:
        env = {"files": [{"project": "alpha-agent", "variables": [
                   {"name": "API_TOKEN", "class": "secret"}, {"name": "DB_URL", "class": "secret"},
                   {"name": "PORT", "class": "plain"}]},
               {"project": "gamma-site", "variables": [{"name": "OTHER", "class": "secret"}]}]}
        got = F.outside_vault(env, {"alpha-agent": {"API_TOKEN"}}, {"alpha-agent": "declared"})
        self.assertEqual(len(got), 1)
        self.assertIn("DB_URL", got[0]["detail"])
        self.assertNotIn("API_TOKEN", got[0]["detail"], "a key already in the vault is fine")
        self.assertIn("vault.py\" put alpha-agent local DB_URL", got[0]["action"])
        self.assertEqual(F.outside_vault(env, {"alpha-agent": {"API_TOKEN", "DB_URL"}},
                                         {"alpha-agent": "declared"}), [])

    def test_a_run_that_fell_back_to_a_dotenv_is_reported(self) -> None:
        rows = [{"action": "use", "subject": "alpha-agent:DB_URL", "from": {"DB_URL": "env:alpha-agent/.env"}},
                {"action": "serve", "subject": "alpha-agent:API_TOKEN",
                 "from": {"API_TOKEN": "vault:alpha-agent/prod/API_TOKEN"}},
                {"action": "use", "subject": "gamma-site:X", "from": {"X": "env:gamma-site/.env"}}]
        got = F.fallback_used(rows, {"alpha-agent": "declared"})
        self.assertEqual([g["subject"] for g in got], ["project-folder:alpha-agent"])
        self.assertIn("DB_URL", got[0]["detail"])
        self.assertNotIn("API_TOKEN", got[0]["detail"])

    def test_a_known_value_in_memory_is_journalled_and_reported(self) -> None:
        state = self.root / "state"
        before = os.environ.get("OBSERVATORY_STATE")
        os.environ["OBSERVATORY_STATE"] = str(state)
        import importlib
        import paths
        importlib.reload(paths)
        try:
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            W.checkpoint_write(conn, owner="agent:fixture", idempotency_key="secrets-0002",
                               step_id="S1", status="done", project_id="project:alpha-agent",
                               body={"goal": f"the token is {VALUE}"},
                               redactor=memory_redact.Redactor(
                                   known_loader=lambda: {VALUE: "vault:alpha-agent/prod/API_TOKEN"}))
            conn.close()
        finally:
            if before is None:
                os.environ.pop("OBSERVATORY_STATE", None)
            else:
                os.environ["OBSERVATORY_STATE"] = before
            importlib.reload(paths)
        journal = (state / "logs" / W.REDACTIONS).read_text(encoding="utf-8")
        self.assertIn("vault:alpha-agent/prod/API_TOKEN", journal)
        self.assertNotIn(VALUE, journal, "the journal names the slot, never the value")
        got = F.findings(db=self.db, state=state, env_doc=None, vault_dir=self.root / "novault",
                         projects=[])
        seen = [g for g in got if g["type"] == "secret.seen_in_agent_memory"]
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["severity"], "critical")
        self.assertIn("vault.py\" leak", seen[0]["action"])
        self.assertNotIn(VALUE, json.dumps(got))

    def test_quiet_when_the_rule_holds(self) -> None:
        self.assertEqual(F.findings(db=self.db, state=self.root / "empty", env_doc={"files": []},
                                    vault_dir=self.root / "novault", projects=[]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
