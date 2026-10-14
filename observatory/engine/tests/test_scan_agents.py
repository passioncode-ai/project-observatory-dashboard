#!/usr/bin/env python3
"""The agents registry: `registry/agents.json`, generated rather than kept by hand.

The estate map says the engine generates the list of agents installed on a machine, and until
OBS-83 nothing did: the projection had no `agents.json` to mirror. This suite drives the three
halves over synthetic inputs only:

* `collectors/scan_agents.py` reads the Fabric service descriptors (fabric-service/0.1) in a
  temporary services directory, each one's Fabric manifest for the contract pins, and the
  launchd map — injected, so `launchctl` never runs here;
* `document()` (called by the emit) joins the MCP inventory by the agent's origin, carries
  first-seen and last-running dates forward, and names everything it could not read;
* the emit writes the document, the validator checks it, and the wiki projection mirrors it
  with no code of its own.

Every name is invented (`alpha-agent`, `beta-agent`, `*.example.com`); no real descriptor,
token or path is read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
for sub in ("collectors", "tools"):
    sys.path.insert(0, str(ROOT / sub))

import scan_agents  # noqa: E402

NOW = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
TODAY = "2026-10-14"
SCHEMA = ROOT / "schemas" / "agents.schema.json"


def descriptor(service_id: str, port: int | None = None, *, instance: str = "default",
               manager: str = "launchd", manifest: str | None = None, remote: str | None = None,
               token: str = "~/fixture/token") -> dict:
    """A fabric-service/0.1 descriptor in the shape `fabric_service.validate_descriptor` accepts."""
    doc = {"protocol": "fabric-service/0.1", "id": service_id, "instance": instance,
           "name": service_id.replace("-", " ").title(), "summary": "A synthetic agent.",
           "auth": {"tokenFile": token},
           "installedAt": "2026-10-01T08:00:00Z", "installedBy": "fixture-installer"}
    if remote:
        doc.update(placement="remote", origin=remote, lifecycle={"manager": "none"})
    else:
        doc.update(origin=f"http://127.0.0.1:{port}",
                   paths={"data": f"~/fixture/{service_id}", "logs": [f"~/fixture/{service_id}/logs"]})
        doc["lifecycle"] = ({"manager": "launchd", "label": f"com.example.{service_id}",
                             "plist": f"~/Library/LaunchAgents/com.example.{service_id}.plist"}
                            if manager == "launchd" else {"manager": manager})
        doc["commands"] = {"doctor": [f"~/fixture/{service_id}/bin/doctor"]}
    if manifest:
        doc["fabricManifest"] = manifest
    return doc


def manifest_doc(provider: str, revision: int = 3) -> dict:
    return {"contractVersion": "0.1.0",
            "provider": {"id": provider, "revision": revision, "contentHash": "sha256:" + "a" * 64,
                         "createdAt": "2026-10-01T08:00:00Z", "createdBy": "fixture",
                         "name": provider, "identity": {"subject": provider, "method": "none"},
                         "supportedContractVersions": ["0.1.0"]},
            "capabilities": []}


def mcp_scan(*servers: dict) -> dict:
    """`store/raw/mcp.json` as `collectors/scan_mcp.py` writes it, reduced to what the join reads."""
    return {"scanned_at": "2026-10-14T09:00:00Z", "servers": list(servers), "degraded": []}


def mcp_server(name: str, target: str | None, agent: str = "claude", scope: str = "user") -> dict:
    return {"name": name, "agent": agent, "scope": scope, "transport": "http" if target else "stdio",
            "target": target, "liveness": "not-probed"}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "user"
        self.home.mkdir()
        self.services = self.base / "services"
        self.services.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(HOME=str(self.home), FABRIC_SERVICES_DIR=str(self.services))
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        (self.home / "Library/LaunchAgents").mkdir(parents=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def put(self, doc: dict, name: str | None = None) -> Path:
        path = self.services / (name or f"{doc['id']}.{doc['instance']}.json")
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def put_manifest(self, provider: str, **kw) -> str:
        path = self.home / "fixture" / provider / "fabric-agent.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest_doc(provider, **kw)), encoding="utf-8")
        return "~/fixture/" + provider + "/fabric-agent.json"

    def scan(self, launchd=None, platform: str = "darwin", launchd_failed: bool = False) -> dict:
        return scan_agents.collect(self.services, launchd=launchd, platform=platform,
                                   launchd_failed=launchd_failed, now=NOW)

    def two(self) -> dict:
        self.put(descriptor("alpha-agent", 47401, manifest=self.put_manifest("alpha-agent")))
        self.put(descriptor("beta-agent", 47402, manifest=self.put_manifest("beta-agent", revision=7)))
        return self.scan(launchd={"com.example.alpha-agent": {"pid": 4242, "status": 0},
                                  "com.example.beta-agent": {"pid": None, "status": 78}})


class TwoDescriptors(Fixture):
    def test_fixture_services_dir_of_two_descriptors_lists_both(self):
        """The node's failing test: two descriptors in, two rows out, with ports and pins."""
        doc = scan_agents.document(self.two(), mcp_scan(), None, TODAY)
        rows = {r["id"]: r for r in doc["agents"]}
        self.assertEqual(sorted(rows), ["agent:alpha-agent.default", "agent:beta-agent.default"])
        self.assertEqual(rows["agent:alpha-agent.default"]["port"], 47401)
        self.assertEqual(rows["agent:beta-agent.default"]["port"], 47402)
        alpha = rows["agent:alpha-agent.default"]
        self.assertEqual(alpha["protocol"], "fabric-service/0.1")
        self.assertEqual(alpha["manifest"]["state"], "pinned")
        self.assertEqual(alpha["manifest"]["contract_version"], "0.1.0")
        self.assertEqual(alpha["manifest"]["provider_revision"], 3)
        self.assertEqual(rows["agent:beta-agent.default"]["manifest"]["provider_revision"], 7)
        self.assertTrue(alpha["manifest"]["content_hash"].startswith("sha256:"))
        self.assertEqual(alpha["supervisor"]["manager"], "launchd")
        self.assertEqual(alpha["supervisor"]["state"], "running")
        self.assertEqual(rows["agent:beta-agent.default"]["supervisor"]["state"], "loaded")
        self.assertEqual(rows["agent:beta-agent.default"]["supervisor"]["last_exit_status"], 78)
        self.assertTrue(all(r["valid"] for r in rows.values()))
        self.assertEqual(doc["totals"]["agents"], 2)
        self.assertEqual(doc["degraded"], [])
        self.assertEqual(scan_agents.problems(doc), [])

    def test_the_rows_carry_no_token_path_and_no_absolute_home_path(self):
        doc = scan_agents.document(self.two(), mcp_scan(), None, TODAY)
        text = json.dumps(doc)
        self.assertNotIn("token", text.lower(), "the token file's location is not an agent fact")
        self.assertNotIn(str(self.home), text)
        self.assertIn("~/fixture/alpha-agent/fabric-agent.json", text)
        self.assertNotIn("bin/doctor", text, "commands are named, never their argv")
        self.assertEqual(doc["agents"][0]["commands"], ["doctor"])

    def test_the_document_matches_its_schema_and_so_does_the_projected_copy(self):
        try:
            import jsonschema
        except ImportError:                                             # pragma: no cover
            self.skipTest("jsonschema is part of the full extra")
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        doc = scan_agents.document(self.two(), mcp_scan(mcp_server("alpha", "http://127.0.0.1:47401/mcp")),
                                   None, TODAY)
        jsonschema.Draft202012Validator(schema).validate(doc)
        projected = {"_generated": True, "_generated_by": "fixture", "_canonical_source": "file:///x",
                     "_do_not_edit": "fixture", **doc}
        jsonschema.Draft202012Validator(schema).validate(projected)
        broken = json.loads(json.dumps(doc))
        broken["agents"][0]["supervisor"]["state"] = "guessed"
        self.assertTrue(list(jsonschema.Draft202012Validator(schema).iter_errors(broken)))


class HonestDegradation(Fixture):
    def test_an_unreadable_descriptor_is_named_and_the_rest_are_still_listed(self):
        self.put(descriptor("alpha-agent", 47401))
        (self.services / "broken-agent.default.json").write_text("{not json", encoding="utf-8")
        doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual([r["id"] for r in doc["agents"]], ["agent:alpha-agent.default"])
        self.assertEqual(len(doc["degraded"]), 1)
        self.assertEqual(doc["degraded"][0]["source"], "fabric-services")
        self.assertIn("broken-agent.default.json", doc["degraded"][0]["reason"])
        self.assertIn("JSONDecodeError", doc["degraded"][0]["reason"])

    def test_an_invalid_descriptor_is_listed_with_its_problems_not_dropped(self):
        bad = descriptor("alpha-agent", 47401)
        bad["lifecycle"] = {"manager": "launchd"}                      # no label, no plist
        self.put(bad)
        doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        row = doc["agents"][0]
        self.assertFalse(row["valid"])
        self.assertTrue(any("label and plist" in p for p in row["problems"]), row["problems"])
        self.assertEqual(doc["totals"]["invalid"], 1)

    def test_a_descriptor_that_is_not_an_object_is_degraded(self):
        (self.services / "list.default.json").write_text("[1, 2]", encoding="utf-8")
        doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"], [])
        self.assertIn("not a JSON object", doc["degraded"][0]["reason"])

    def test_two_descriptors_claiming_one_id_keep_the_first_and_name_the_second(self):
        self.put(descriptor("alpha-agent", 47401), "alpha-agent.default.json")
        self.put(descriptor("alpha-agent", 47409), "zz-copy.json")
        doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual([r["port"] for r in doc["agents"]], [47401])
        self.assertTrue(any("zz-copy.json" in d["reason"] for d in doc["degraded"]))
        self.assertEqual(scan_agents.problems(doc), [])

    def test_an_absent_services_directory_is_an_empty_machine_not_a_partial_scan(self):
        self.services.rmdir()
        scan = self.scan(launchd={})
        self.assertEqual(scan["sources"]["fabric_services"]["state"], "absent")
        doc = scan_agents.document(scan, mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"], [])
        self.assertEqual(doc["degraded"], [])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "permission bits do not bind here")
    def test_an_unreadable_services_directory_keeps_the_last_rows_and_says_so(self):
        previous = scan_agents.document(self.two(), mcp_scan(), None, "2026-10-13")
        self.services.chmod(0)
        try:
            doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), previous, TODAY)
        finally:
            self.services.chmod(0o700)
        self.assertEqual(len(doc["agents"]), 2, "an empty list in place of a partial one is a false claim")
        self.assertTrue(all(r["carried"] for r in doc["agents"]))
        self.assertEqual(doc["degraded"][0]["source"], "fabric-services")
        self.assertEqual(doc["removed"], [])

    def test_an_unreadable_manifest_is_degraded_and_the_row_says_unreadable(self):
        self.put(descriptor("alpha-agent", 47401, manifest="~/fixture/alpha-agent/missing.json"))
        doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["manifest"]["state"], "unreadable")
        self.assertEqual(doc["degraded"][0]["source"], "fabric-manifest")
        self.assertIn("FileNotFoundError", doc["degraded"][0]["reason"])

    def test_no_manifest_declared_is_absent_not_degraded(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["manifest"], {"state": "absent"})
        self.assertEqual(doc["degraded"], [])


class Supervisor(Fixture):
    def test_a_label_launchd_does_not_list_is_not_loaded_and_the_plist_is_checked(self):
        self.put(descriptor("alpha-agent", 47401))
        (self.home / "Library/LaunchAgents/com.example.alpha-agent.plist").write_text("<plist/>")
        row = scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)["agents"][0]
        self.assertEqual(row["supervisor"]["state"], "not-loaded")
        self.assertTrue(row["supervisor"]["plist_present"])
        self.assertEqual(row["supervisor"]["plist"], "~/Library/LaunchAgents/com.example.alpha-agent.plist")

    def test_launchctl_failing_on_macos_degrades_and_measures_nothing(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = scan_agents.document(self.scan(launchd=None, launchd_failed=True), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["supervisor"]["state"], "not-measured")
        self.assertEqual([d["source"] for d in doc["degraded"]], ["launchd"])

    def test_launchd_on_another_platform_is_not_measured_and_not_degraded(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd=None, platform="linux")
        self.assertEqual(scan["sources"]["launchd"]["state"], "not-applicable")
        doc = scan_agents.document(scan, mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["supervisor"]["state"], "not-measured")
        self.assertEqual(doc["degraded"], [])

    def test_systemd_is_named_as_not_read_and_a_remote_agent_is_its_platforms(self):
        self.put(descriptor("alpha-agent", 47401, manager="systemd"))
        self.put(descriptor("beta-agent", remote="https://beta.example.com"))
        rows = {r["service_id"]: r for r in
                scan_agents.document(self.scan(launchd={}), mcp_scan(), None, TODAY)["agents"]}
        self.assertEqual(rows["alpha-agent"]["supervisor"]["state"], "not-measured")
        self.assertIn("systemd", rows["alpha-agent"]["supervisor"]["why"])
        self.assertEqual(rows["beta-agent"]["supervisor"]["state"], "platform")
        self.assertEqual(rows["beta-agent"]["placement"], "remote")
        self.assertIsNone(rows["beta-agent"]["port"])
        self.assertEqual(rows["beta-agent"]["host"], "beta.example.com")

    def test_launchctl_list_is_parsed_for_pid_and_status(self):
        out = "PID\tStatus\tLabel\n4242\t0\tcom.example.alpha-agent\n-\t78\tcom.example.beta-agent\nbad line\n"
        self.assertEqual(scan_agents.parse_launchctl(out),
                         {"com.example.alpha-agent": {"pid": 4242, "status": 0},
                          "com.example.beta-agent": {"pid": None, "status": 78}})


class McpJoin(Fixture):
    def test_mcp_declarations_join_by_origin_and_use_the_inventorys_ids(self):
        import estate_surfaces
        self.put(descriptor("alpha-agent", 47401))
        self.put(descriptor("beta-agent", remote="https://beta.example.com"))
        scan = mcp_scan(mcp_server("alpha", "http://127.0.0.1:47401/mcp"),
                        mcp_server("alpha", "http://localhost:47401/mcp", agent="cursor"),
                        mcp_server("beta-remote", "https://beta.example.com/mcp"),
                        mcp_server("elsewhere", "http://127.0.0.1:9999/mcp"),
                        mcp_server("local-tool", None))
        doc = scan_agents.document(self.scan(launchd={}), scan, None, TODAY)
        rows = {r["service_id"]: r for r in doc["agents"]}
        ids = {r["id"] for r in estate_surfaces.mcp_rows(scan)}
        self.assertEqual(rows["alpha-agent"]["mcp_declarations"],
                         ["mcp:claude/user/alpha", "mcp:cursor/user/alpha"])
        self.assertEqual(rows["beta-agent"]["mcp_declarations"], ["mcp:claude/user/beta-remote"])
        self.assertTrue(set(rows["alpha-agent"]["mcp_declarations"]) <= ids)
        self.assertEqual(doc["totals"]["mcp"], {"attributed": 3, "unattributed_http": 1, "stdio": 1})
        self.assertIn("SRC-0017", doc["source_refs"])

    def test_without_an_mcp_inventory_the_join_is_unmeasured_not_empty(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = scan_agents.document(self.scan(launchd={}), None, None, TODAY)
        self.assertIsNone(doc["agents"][0]["mcp_declarations"])
        self.assertIsNone(doc["totals"]["mcp"])
        self.assertEqual([d["source"] for d in doc["degraded"]], ["mcp-inventory"])
        self.assertNotIn("SRC-0017", doc["source_refs"])


class History(Fixture):
    def test_first_seen_and_last_running_carry_forward_and_a_removed_agent_is_named(self):
        first = scan_agents.document(self.two(), mcp_scan(), None, "2026-10-12")
        (self.services / "beta-agent.default.json").unlink()
        self.put(descriptor("gamma-agent", 47403))
        later = scan_agents.document(self.scan(launchd={"com.example.alpha-agent": {"pid": None, "status": 0}}),
                                     mcp_scan(), first, TODAY)
        rows = {r["service_id"]: r for r in later["agents"]}
        self.assertEqual(rows["alpha-agent"]["first_seen_on"], "2026-10-12")
        self.assertEqual(rows["alpha-agent"]["last_seen_running_on"], "2026-10-12",
                         "not running now: the last day it was seen running is kept")
        self.assertEqual(rows["gamma-agent"]["first_seen_on"], TODAY)
        self.assertIsNone(rows["gamma-agent"]["last_seen_running_on"])
        self.assertEqual(later["removed"], ["agent:beta-agent.default"])

    def test_the_same_machine_twice_is_the_same_document(self):
        """No instant or pid in the document, so an unchanged estate does not rewrite the registry."""
        a = scan_agents.document(self.two(), mcp_scan(), None, TODAY)
        b = scan_agents.document(self.two(), mcp_scan(), a, TODAY)
        self.assertEqual(a, b)
        self.assertNotIn("4242", json.dumps(a))


class Validator(Fixture):
    def test_the_validator_rules_catch_what_the_schema_promises(self):
        doc = scan_agents.document(self.two(), mcp_scan(), None, TODAY)
        planted = json.loads(json.dumps(doc))
        planted["agents"][1]["id"] = planted["agents"][0]["id"]
        self.assertTrue(any("duplicate" in p for p in scan_agents.problems(planted)))
        planted = json.loads(json.dumps(doc))
        planted["agents"][0]["auth"] = {"tokenFile": "~/x"}
        self.assertTrue(any("auth" in p for p in scan_agents.problems(planted)))
        planted = json.loads(json.dumps(doc))
        planted["agents"][0]["manifest"]["path"] = str(self.home / "x.json")
        self.assertTrue(any("~/" in p for p in scan_agents.problems(planted)))
        planted = json.loads(json.dumps(doc))
        planted["totals"]["agents"] = 5
        self.assertTrue(any("totals" in p for p in scan_agents.problems(planted)))
        planted = json.loads(json.dumps(doc))
        planted["source_refs"] = []
        self.assertTrue(any("SRC-0020" in p for p in scan_agents.problems(planted)))


class Pipeline(Fixture):
    """The collector, the emit and the projection, as the tick runs them."""

    def test_the_collector_writes_its_scan(self):
        self.put(descriptor("alpha-agent", 47401))
        out = self.base / "raw" / "agents.json"
        env = {**os.environ, "OBSERVATORY_AGENTS_LAUNCHD": "skip"}
        p = subprocess.run([sys.executable, "collectors/scan_agents.py", str(out)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        scan = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([s["id"] for s in scan["services"]], ["agent:alpha-agent.default"])
        self.assertIn("1 agent", p.stdout)

    def test_the_emit_writes_registry_agents_and_the_projection_mirrors_it_unchanged(self):
        import workspace
        from emitter_fixture import environment, seed
        root = self.base / "estate"
        root.mkdir()
        seed(root)
        workspace.initialize(root / "home", identities=False)
        settings = root / "home" / "config" / "settings.json"
        conf = json.loads(settings.read_text(encoding="utf-8"))
        conf.setdefault("features", {})["wiki_projection"] = True
        settings.write_text(json.dumps(conf), encoding="utf-8")
        self.put(descriptor("alpha-agent", 47401, manifest=self.put_manifest("alpha-agent")))
        self.put(descriptor("beta-agent", 47402))
        raw, reg = root / "raw", root / "registry"
        scan = self.scan(launchd={"com.example.alpha-agent": {"pid": 1, "status": 0}})
        (raw / "agents.json").write_text(json.dumps(scan), encoding="utf-8")
        (raw / "mcp.json").write_text(json.dumps(mcp_scan(mcp_server("alpha", "http://127.0.0.1:47401/mcp"))),
                                      encoding="utf-8")
        env = {**environment(root), "OBSERVATORY_HOME": str(root / "home")}
        p = subprocess.run([sys.executable, "collectors/emit_registry.py", str(raw)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-600:])
        self.assertIn("agents.json: 2 agent(s)", p.stdout)
        doc = json.loads((reg / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in doc["agents"]],
                         ["agent:alpha-agent.default", "agent:beta-agent.default"])
        self.assertEqual(doc["agents"][0]["mcp_declarations"], ["mcp:claude/user/alpha"])
        self.assertEqual(scan_agents.problems(doc), [])
        sources = {s["id"]: s for s in json.loads((reg / "sources.json").read_text(encoding="utf-8"))["sources"]}
        self.assertEqual(sources["SRC-0020"]["evidence_for"], ["registry:agents.json"])

        vault = root / "vault"
        (vault / "inventory").mkdir(parents=True)
        p = subprocess.run([sys.executable, "tools/project_into_vault.py"], cwd=ROOT,
                           env={**env, "OBSERVATORY_VAULT": str(vault)},
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-400:])
        mirrored = json.loads((vault / "inventory" / "agents.json").read_text(encoding="utf-8"))
        self.assertTrue(mirrored.pop("_generated"))
        for key in ("_generated_by", "_canonical_source", "_do_not_edit"):
            mirrored.pop(key)
        self.assertEqual(mirrored, doc, "the projection adds its header and changes nothing else")

    def test_the_tick_and_the_cli_run_the_collector(self):
        import tick_reader
        calls = [words for _line, _fn, _name, words in tick_reader.tick_calls(ROOT)]
        self.assertTrue(any("collectors/scan_agents.py" in w for words in calls for w in words),
                        "the tick runs scan_agents")
        sys.path.insert(0, str(ROOT))
        src = (ROOT / "observatory.py").read_text(encoding="utf-8")
        self.assertIn('"scan-agents":', src)


if __name__ == "__main__":
    unittest.main()
