#!/usr/bin/env python3
"""`machine.mcp.inventory`: the last MCP scan, as the shape a host joins on.

The capability answers from the scan the tick already takes (`store/raw/mcp.json`),
so these cases build that file the way the collector does — from planted agent
configs and a stand-in `claude` — and read the answer back. Four properties are
the point:

- no planted credential, URL, argument or home path reaches the answer;
- a source that is absent or unreadable is NAMED in `degraded`, never dropped;
- an inventory older than the tick's own staleness bound is called stale;
- a machine never scanned answers with an empty list and says why, rather than
  looking like a machine with no MCP servers.

Everything runs in a temporary HOME and workspace; nothing outside them is read.
"""
from __future__ import annotations
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "collectors"))
import mcp_config_fixture as fx                                          # noqa: E402

SCHEMA = ROOT / "fabric/schemas/mcp-inventory-output.schema.json"
PROBE = ("alpha-docs: https://docs.example.com/mcp (HTTP) - ✔ Connected\n"
         "beta-events: https://events.example.com/sse (SSE) - ✘ Failed to connect\n"
         "gamma-local: /opt/example/bin/gamma-mcp --api-key planted-probe-arg-8d7c6b5a - ✔ Connected\n"
         "plugin:omega:omega-api: https://omega.example.com/mcp?token=planted-probe-query-4a3b2c1d (HTTP)"
         " - ! Needs authentication\n")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Sandbox(unittest.TestCase):
    """A private HOME, a workspace and an agent-config home with planted secrets."""

    mcp_enabled = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.agents = self.base / "agents"
        fx.make_workspace(self.home, mcp_root=self.agents if self.mcp_enabled else None)
        (self.base / "user").mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(HOME=str(self.base / "user"), OBSERVATORY_HOME=str(self.home),
                   PYTHONDONTWRITEBYTECODE="1")
        self.env = env
        self.patch = mock.patch.dict(os.environ, env, clear=True)
        self.patch.start()
        import paths
        self.paths = importlib.reload(paths)
        import scan_mcp
        self.scan_mcp = importlib.reload(scan_mcp)
        import mcp_inventory
        self.inv = importlib.reload(mcp_inventory)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def write_scan(self, *, at: datetime | None = None, skip=(), **mutate) -> dict:
        fx.write_home(self.agents, skip=skip)
        doc = self.scan_mcp.scan(self.agents, probe=lambda: (self.scan_mcp.parse_probe(PROBE), None, True))
        if at is not None:
            doc["scanned_at"] = iso(at)
        doc.update(mutate)
        target = self.paths.SCRATCH / "mcp.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")
        return doc

    def validate(self, answer: dict) -> None:
        import jsonschema
        jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")),
                                        format_checker=jsonschema.FormatChecker()).validate(answer)


class TheAnswer(Sandbox):
    def test_servers_are_grouped_by_name_and_transport_with_every_declaration(self):
        self.write_scan()
        answer = self.inv.inventory()
        self.validate(answer)
        by = {(s["name"], s["transport"]): s for s in answer["servers"]}
        docs = by[("alpha-docs", "streamable-http")]
        self.assertEqual(sorted(d["agent"] for d in docs["declaredIn"]), ["claude-code", "cursor-agent"])
        self.assertEqual({d["file"] for d in docs["declaredIn"]}, {"~/.claude.json", "~/.cursor/mcp.json"})
        self.assertIs(docs["answers"], True)
        self.assertEqual(docs["checkedAt"], answer["inventoryAt"])
        self.assertIs(by[("beta-events", "sse")]["answers"], False)
        epsilon = by[("epsilon-tool", "stdio")]
        self.assertIsNone(epsilon["answers"], "Cursor has no probe; its verdict is unknown, not borrowed")
        self.assertIsNone(epsilon["checkedAt"])
        self.assertEqual(epsilon["status"], "not-probed")

    def test_a_server_that_answered_asking_for_authorization_answers(self):
        self.write_scan()
        omega = next(s for s in self.inv.inventory()["servers"] if s["name"] == "omega-api")
        self.assertIs(omega["answers"], True)
        self.assertEqual(omega["status"], "needs-auth")
        self.assertEqual(omega["declaredIn"], [{"agent": "claude-code", "file": None, "scope": "plugin:omega",
                                                "status": "needs-auth", "disabled": False}])
        self.assertEqual(omega["transport"], "streamable-http")

    def test_a_server_switched_off_in_its_config_is_listed_as_disabled(self):
        self.write_scan()
        eta = next(s for s in self.inv.inventory()["servers"] if s["name"] == "eta-local")
        self.assertEqual(eta["status"], "disabled")
        self.assertIsNone(eta["answers"])
        self.assertTrue(eta["declaredIn"][0]["disabled"])

    def test_every_agent_kind_is_the_runner_catalogue_kind(self):
        self.write_scan()
        kinds = {d["agent"] for s in self.inv.inventory()["servers"] for d in s["declaredIn"]}
        self.assertEqual(kinds, {"claude-code", "cursor-agent", "opencode", "codex", "gemini-cli", "kiro"})

    def test_a_project_scoped_declaration_keeps_its_scope(self):
        self.write_scan()
        delta = next(s for s in self.inv.inventory()["servers"] if s["name"] == "delta-project")
        self.assertEqual(delta["declaredIn"][0]["scope"], "project:~/work/alpha-web")
        self.assertEqual(delta["declaredIn"][0]["file"], "~/.claude.json")
        self.assertEqual(delta["status"], "not-probed", "out of the probe's view, not down")


class NothingSensitive(Sandbox):
    def test_no_planted_value_url_argument_or_home_path_reaches_the_answer(self):
        self.write_scan()
        text = json.dumps(self.inv.inventory())
        self.assertEqual(fx.leaked(text), [])
        self.assertNotIn("planted", text)
        self.assertNotIn(str(self.base), text)
        for needle in ("example.com", "--api-key", "gamma-mcp", "Bearer", "token="):
            self.assertNotIn(needle, text, f"{needle!r} is a target, command or header, not a name")

    def test_only_the_declared_fields_leave_the_builder(self):
        """Redaction by construction: a field the scan grows later does not reach a host."""
        doc = self.write_scan()
        doc["servers"][0]["target"] = "https://planted-new-field.example.com/mcp?key=planted-new"
        doc["servers"][0]["future_field"] = "planted-future"
        (self.paths.SCRATCH / "mcp.json").write_text(json.dumps(doc), encoding="utf-8")
        answer = self.inv.inventory()
        self.assertNotIn("planted", json.dumps(answer))
        for server in answer["servers"]:
            self.assertLessEqual(set(server), {"name", "declaredIn", "transport", "answers", "status", "checkedAt"})

    def test_the_live_collector_path_never_writes_a_value(self):
        """The collector as the tick runs it: a real process, a stand-in `claude`, planted configs."""
        fx.write_home(self.agents)
        bin_dir = fx.stub_claude(self.base / "bin").parent
        env = {**self.env, "PATH": f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}"}
        out = self.paths.SCRATCH / "mcp.json"
        run = subprocess.run([sys.executable, "collectors/scan_mcp.py", str(out)], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        raw = out.read_text(encoding="utf-8")
        self.assertEqual(fx.leaked(raw), [], "the scan file itself holds no planted value")
        self.assertNotIn("planted", raw)
        self.assertNotIn("planted", run.stdout + run.stderr, "nor does the collector's own output")
        answer = self.inv.inventory()
        self.validate(answer)
        self.assertNotIn("planted", json.dumps(answer))
        self.assertTrue(answer["servers"])


class Coverage(Sandbox):
    def test_absent_and_unreadable_sources_are_named_in_degraded(self):
        fx.write_home(self.agents, skip=("gemini", "kiro"))
        (self.agents / ".cursor/mcp.json").write_text("{not json", encoding="utf-8")
        doc = self.scan_mcp.scan(self.agents, probe=lambda: ({}, None, True))
        (self.paths.SCRATCH / "mcp.json").write_text(json.dumps(doc), encoding="utf-8")
        answer = self.inv.inventory()
        self.validate(answer)
        reasons = {d["source"]: d["reason"] for d in answer["degraded"]}
        self.assertIn("gemini-cli:~/.gemini/settings.json", reasons)
        self.assertTrue(reasons["gemini-cli:~/.gemini/settings.json"].startswith("absent"))
        self.assertIn("kiro:~/.kiro/settings/mcp.json", reasons)
        self.assertTrue(reasons["cursor-agent:~/.cursor/mcp.json"].startswith("unreadable"))
        self.assertEqual(sum(1 for d in answer["degraded"] if d["source"].startswith("cursor-agent:")), 1,
                         "one row per unreadable file, not one from the scan and one from the sources")
        states = {s["agent"]: s["state"] for s in answer["sources"]}
        self.assertEqual(states["gemini-cli"], "absent")
        self.assertEqual(states["cursor-agent"], "unreadable")

    def test_a_probe_the_scan_could_not_finish_is_carried_through(self):
        self.write_scan(degraded=[{"source": "claude mcp list", "reason": "did not finish within 2 s"}])
        reasons = {d["source"] for d in self.inv.inventory()["degraded"]}
        self.assertIn("claude mcp list", reasons)

    def test_a_fresh_inventory_with_every_source_read_is_not_degraded(self):
        self.write_scan(at=datetime.now(timezone.utc) - timedelta(minutes=5))
        self.assertEqual(self.inv.inventory()["degraded"], [])


class Freshness(Sandbox):
    def test_an_inventory_older_than_the_tick_bound_is_stale(self):
        import tick_health
        at = datetime.now(timezone.utc) - tick_health.STALE_AFTER - timedelta(minutes=10)
        self.write_scan(at=at)
        answer = self.inv.inventory()
        self.assertEqual(answer["inventoryAt"], iso(at), "the old answer is still given, with its time")
        stale = [d for d in answer["degraded"] if d["source"] == "mcp inventory"]
        self.assertEqual(len(stale), 1, answer["degraded"])
        self.assertIn("stale", stale[0]["reason"])
        self.assertIn(iso(at), stale[0]["reason"])
        self.assertTrue(answer["servers"], "stale is not empty: the last inventory is shown")

    def test_one_just_inside_the_bound_is_not(self):
        import tick_health
        self.write_scan(at=datetime.now(timezone.utc) - tick_health.STALE_AFTER + timedelta(minutes=10))
        self.assertFalse([d for d in self.inv.inventory()["degraded"] if d["source"] == "mcp inventory"])

    def test_an_unparseable_scan_time_is_not_called_fresh(self):
        self.write_scan(scanned_at="yesterday-ish")
        answer = self.inv.inventory()
        self.assertIsNone(answer["inventoryAt"])
        self.assertTrue(any(d["source"] == "mcp inventory" for d in answer["degraded"]))
        self.validate(answer)


class NeverScanned(Sandbox):
    def test_no_scan_is_an_empty_answer_that_says_why(self):
        answer = self.inv.inventory()
        self.validate(answer)
        self.assertEqual(answer["servers"], [])
        self.assertIsNone(answer["inventoryAt"])
        self.assertEqual([d["source"] for d in answer["degraded"]], ["mcp inventory"])
        self.assertIn("never", answer["degraded"][0]["reason"])

    def test_an_unreadable_scan_file_is_named_not_treated_as_empty(self):
        (self.paths.SCRATCH / "mcp.json").write_text("{", encoding="utf-8")
        answer = self.inv.inventory()
        self.validate(answer)
        self.assertEqual(answer["servers"], [])
        self.assertIn("unreadable", answer["degraded"][0]["reason"])

    def test_a_scan_of_the_older_shape_still_answers(self):
        """Scans written before sources and wire existed: http maps to streamable-http."""
        old = {"scanned_at": iso(datetime.now(timezone.utc)), "own_server": "observatory", "degraded": [],
               "servers": [{"name": "alpha-docs", "agent": "claude", "scope": "user", "transport": "http",
                            "declared_in": "~/.claude.json", "liveness": "connected"},
                           {"name": None, "agent": "opencode", "error": "ValueError"}]}
        (self.paths.SCRATCH / "mcp.json").write_text(json.dumps(old), encoding="utf-8")
        answer = self.inv.inventory()
        self.validate(answer)
        self.assertEqual(answer["servers"][0]["transport"], "streamable-http")
        self.assertTrue(any(d["source"].startswith("opencode") for d in answer["degraded"]))


class Disabled(Sandbox):
    mcp_enabled = False

    def test_a_workspace_without_the_integration_says_so(self):
        answer = self.inv.inventory()
        self.validate(answer)
        self.assertEqual(answer["servers"], [])
        reasons = " ".join(d["reason"] for d in answer["degraded"])
        self.assertIn("integration", reasons)
        self.assertIn("mcp_config_root", reasons)


if __name__ == "__main__":
    unittest.main(verbosity=2)
