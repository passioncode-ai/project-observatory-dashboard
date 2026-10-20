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
import re
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


def mcp_server(name: str, target: str | None, agent: str = "claude", scope: str = "user",
               command: str | None = None) -> dict:
    """A declaration as `scan_mcp.declaration` writes it: a stdio one's target is its command's
    basename (`scan_mcp.py`, `target = pathlib.Path(cmd).name`), never empty."""
    if command:
        return {"name": name, "agent": agent, "scope": scope, "transport": "stdio", "target": command,
                "command": command, "liveness": "not-probed"}
    return {"name": name, "agent": agent, "scope": scope, "transport": "http",
            "target": target, "liveness": "not-probed"}


def keyed(secret: str, rest: str) -> str:
    """`https://u:<secret>` + `@` + rest, assembled at run time: the public-release gate refuses a
    credential-shaped URL literal anywhere in the tree or its history, fixtures included."""
    return "https://u:" + secret + chr(64) + rest


def document(scan: dict, mcp: dict | None, previous: dict | None, day: str = TODAY,
             now: datetime | None = None) -> dict:
    """`scan_agents.document` at the instant the scan was taken unless a test says otherwise."""
    return scan_agents.document(scan, mcp, previous, day, now=now or NOW)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "user"
        self.home.mkdir()
        self.services = self.base / "services"
        self.services.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        # USERPROFILE too: Windows resolves `~` and Path.home() from it, not from HOME.
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), FABRIC_SERVICES_DIR=str(self.services))
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

    def scan(self, launchd=None, platform: str = "darwin", launchd_failed: bool = False,
             now: datetime | None = None) -> dict:
        return scan_agents.collect(self.services, launchd=launchd, platform=platform,
                                   launchd_failed=launchd_failed, now=now or NOW)

    def two(self) -> dict:
        self.put(descriptor("alpha-agent", 47401, manifest=self.put_manifest("alpha-agent")))
        self.put(descriptor("beta-agent", 47402, manifest=self.put_manifest("beta-agent", revision=7)))
        return self.scan(launchd={"com.example.alpha-agent": {"pid": 4242, "status": 0},
                                  "com.example.beta-agent": {"pid": None, "status": 78}})


class TwoDescriptors(Fixture):
    def test_fixture_services_dir_of_two_descriptors_lists_both(self):
        """The node's failing test: two descriptors in, two rows out, with ports and pins."""
        doc = document(self.two(), mcp_scan(), None, TODAY)
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
        doc = document(self.two(), mcp_scan(), None, TODAY)
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
        doc = document(self.two(), mcp_scan(mcp_server("alpha", "http://127.0.0.1:47401/mcp")),
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
        doc = document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual([r["id"] for r in doc["agents"]], ["agent:alpha-agent.default"])
        self.assertEqual(len(doc["degraded"]), 1)
        self.assertEqual(doc["degraded"][0]["source"], "fabric-services")
        self.assertIn("broken-agent.default.json", doc["degraded"][0]["reason"])
        self.assertIn("JSONDecodeError", doc["degraded"][0]["reason"])

    def test_an_invalid_descriptor_is_listed_with_its_problems_not_dropped(self):
        bad = descriptor("alpha-agent", 47401)
        bad["lifecycle"] = {"manager": "launchd"}                      # no label, no plist
        self.put(bad)
        doc = document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        row = doc["agents"][0]
        self.assertFalse(row["valid"])
        self.assertTrue(any("label and plist" in p for p in row["problems"]), row["problems"])
        self.assertEqual(doc["totals"]["invalid"], 1)

    def test_a_descriptor_that_is_not_an_object_is_degraded(self):
        (self.services / "list.default.json").write_text("[1, 2]", encoding="utf-8")
        doc = document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"], [])
        self.assertIn("not a JSON object", doc["degraded"][0]["reason"])

    def test_two_descriptors_claiming_one_id_keep_the_first_and_name_the_second(self):
        self.put(descriptor("alpha-agent", 47401), "alpha-agent.default.json")
        self.put(descriptor("alpha-agent", 47409), "zz-copy.json")
        doc = document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual([r["port"] for r in doc["agents"]], [47401])
        self.assertTrue(any("zz-copy.json" in d["reason"] for d in doc["degraded"]))
        self.assertEqual(scan_agents.problems(doc), [])

    def test_an_absent_services_directory_is_an_empty_machine_not_a_partial_scan(self):
        self.services.rmdir()
        scan = self.scan(launchd={})
        self.assertEqual(scan["sources"]["fabric_services"]["state"], "absent")
        doc = document(scan, mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"], [])
        self.assertEqual(doc["degraded"], [])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "permission bits do not bind here")
    def test_an_unreadable_services_directory_keeps_the_last_rows_and_says_so(self):
        previous = document(self.two(), mcp_scan(), None)
        self.services.chmod(0)
        try:
            doc = document(self.scan(launchd={}), mcp_scan(), previous, TODAY)
        finally:
            self.services.chmod(0o700)
        self.assertEqual(len(doc["agents"]), 2, "an empty list in place of a partial one is a false claim")
        self.assertTrue(all(r["carried"] for r in doc["agents"]))
        self.assertEqual(doc["degraded"][0]["source"], "fabric-services")
        self.assertEqual(doc["removed"], [])

    def test_an_unreadable_manifest_is_degraded_and_the_row_says_unreadable(self):
        self.put(descriptor("alpha-agent", 47401, manifest="~/fixture/alpha-agent/missing.json"))
        doc = document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["manifest"]["state"], "unreadable")
        self.assertEqual(doc["degraded"][0]["source"], "fabric-manifest")
        self.assertIn("FileNotFoundError", doc["degraded"][0]["reason"])

    def test_no_manifest_declared_is_absent_not_degraded(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = document(self.scan(launchd={}), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["manifest"], {"state": "absent"})
        self.assertEqual(doc["degraded"], [])


class Supervisor(Fixture):
    def test_a_label_launchd_does_not_list_is_not_loaded_and_the_plist_is_checked(self):
        self.put(descriptor("alpha-agent", 47401))
        (self.home / "Library/LaunchAgents/com.example.alpha-agent.plist").write_text("<plist/>")
        row = document(self.scan(launchd={}), mcp_scan(), None, TODAY)["agents"][0]
        self.assertEqual(row["supervisor"]["state"], "not-loaded")
        self.assertTrue(row["supervisor"]["plist_present"])
        self.assertEqual(row["supervisor"]["plist"], "~/Library/LaunchAgents/com.example.alpha-agent.plist")

    def test_launchctl_failing_on_macos_degrades_and_measures_nothing(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = document(self.scan(launchd=None, launchd_failed=True), mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["supervisor"]["state"], "not-measured")
        self.assertEqual([d["source"] for d in doc["degraded"]], ["launchd"])

    def test_launchd_on_another_platform_is_not_measured_and_not_degraded(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd=None, platform="linux")
        self.assertEqual(scan["sources"]["launchd"]["state"], "not-applicable")
        doc = document(scan, mcp_scan(), None, TODAY)
        self.assertEqual(doc["agents"][0]["supervisor"]["state"], "not-measured")
        self.assertEqual(doc["degraded"], [])

    def test_systemd_is_named_as_not_read_and_a_remote_agent_is_its_platforms(self):
        self.put(descriptor("alpha-agent", 47401, manager="systemd"))
        self.put(descriptor("beta-agent", remote="https://beta.example.com"))
        rows = {r["service_id"]: r for r in
                document(self.scan(launchd={}), mcp_scan(), None, TODAY)["agents"]}
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
                        mcp_server("local-tool", None, command="alpha-tool"))
        doc = document(self.scan(launchd={}), scan, None, TODAY)
        rows = {r["service_id"]: r for r in doc["agents"]}
        ids = {r["id"] for r in estate_surfaces.mcp_rows(scan)}
        self.assertEqual(rows["alpha-agent"]["mcp_declarations"],
                         ["mcp:claude/user/alpha", "mcp:cursor/user/alpha"])
        self.assertEqual(rows["beta-agent"]["mcp_declarations"], ["mcp:claude/user/beta-remote"])
        self.assertTrue(set(rows["alpha-agent"]["mcp_declarations"]) <= ids)
        self.assertEqual(doc["totals"]["mcp"], {"attributed": 3, "ambiguous": 0, "unattributed_http": 1, "stdio": 1, "other": 0})
        self.assertIn("SRC-0017", doc["source_refs"])

    def test_without_an_mcp_inventory_the_join_is_unmeasured_not_empty(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = document(self.scan(launchd={}), None, None, TODAY)
        self.assertIsNone(doc["agents"][0]["mcp_declarations"])
        self.assertIsNone(doc["totals"]["mcp"])
        self.assertEqual([d["source"] for d in doc["degraded"]], ["mcp-inventory"])
        self.assertNotIn("SRC-0017", doc["source_refs"])


class History(Fixture):
    def test_first_seen_and_last_running_carry_forward_and_a_removed_agent_is_named(self):
        earlier = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
        self.put(descriptor("alpha-agent", 47401, manifest=self.put_manifest("alpha-agent")))
        self.put(descriptor("beta-agent", 47402, manifest=self.put_manifest("beta-agent", revision=7)))
        first = document(self.scan(launchd={"com.example.alpha-agent": {"pid": 4242, "status": 0}}, now=earlier),
                         mcp_scan(), None, "2026-10-12", now=earlier)
        (self.services / "beta-agent.default.json").unlink()
        self.put(descriptor("gamma-agent", 47403))
        later = document(self.scan(launchd={"com.example.alpha-agent": {"pid": None, "status": 0}}),
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
        a = document(self.two(), mcp_scan(), None, TODAY)
        b = document(self.two(), mcp_scan(), a, TODAY)
        self.assertEqual(a, b)
        self.assertNotIn("4242", json.dumps(a))


class Validator(Fixture):
    def test_the_validator_rules_catch_what_the_schema_promises(self):
        doc = document(self.two(), mcp_scan(), None, TODAY)
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
        scan = self.scan(launchd={"com.example.alpha-agent": {"pid": 1, "status": 0}},
                         now=datetime.now(timezone.utc))
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
        self.assertFalse(doc["stale"], "a scan taken this tick is fresh")
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


class FixCycle1(Fixture):
    """Review round 1 of #288: each case fails against ab67476f."""

    # --- MEDIUM: totals.mcp counted stdio by an empty target -----------------------------
    def test_a_stdio_declaration_with_its_command_basename_counts_as_stdio(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = document(self.scan(launchd={}), mcp_scan(mcp_server("tool", None, command="alpha-tool")), None)
        self.assertEqual(doc["totals"]["mcp"]["stdio"], 1)
        self.assertEqual(doc["totals"]["mcp"]["unattributed_http"], 0)

    # --- M1: URL credentials copied verbatim ------------------------------------------------
    def test_user_info_and_queries_never_reach_the_document(self):
        secrets = ["SECRETA1", "SECRETB2", "SECRETC3", "SECRETD4", "SECRETE5"]
        local = descriptor("alpha-agent", 47401)
        local["source"] = {"repository": keyed(secrets[0], "github.example.com/x/y") + f"?token={secrets[1]}"}
        local["paths"]["data"] = keyed(secrets[4], "share.example.com/data")          # echoed by %r
        remote = descriptor("beta-agent", remote="https://beta.example.com")
        remote["origin"] = keyed(secrets[2], "beta.example.com") + f"?k={secrets[3]}"
        self.put(local)
        self.put(remote)
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        text = json.dumps(doc)
        for secret in secrets:
            self.assertEqual(text.count(secret), 0, secret)
        rows = {r["service_id"]: r for r in doc["agents"]}
        self.assertTrue(rows["alpha-agent"]["key_in_url"])
        self.assertIsNone(rows["alpha-agent"]["source_repository"], "refused whole, never cleaned")
        self.assertTrue(rows["beta-agent"]["key_in_url"])
        self.assertIsNone(rows["beta-agent"]["origin"], "refused whole, never cleaned")
        self.assertTrue(any(d["reason"].startswith("origin unreadable") and d.get("agent") == rows["beta-agent"]["id"]
                            for d in doc["degraded"]))
        self.assertEqual(scan_agents.problems(doc), [])

    def test_problems_refuses_a_url_with_user_info_or_a_query(self):
        self.put(descriptor("alpha-agent", 47401))
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        for planted in (keyed("pw", "github.example.com/x"), "https://github.example.com/x?token=t"):
            bad = json.loads(json.dumps(doc))
            bad["agents"][0]["source_repository"] = planted
            self.assertTrue(any("user-info or a query" in p for p in scan_agents.problems(bad)), planted)

    # --- M2: a stale scan re-emitted as fresh ---------------------------------------------
    def test_an_unchanged_old_scan_is_stale_and_dates_nothing_after_it(self):
        old = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd={"com.example.alpha-agent": {"pid": 7, "status": 0}}, now=old)
        day14 = document(scan, mcp_scan(), None, "2026-10-14", now=NOW)
        row = day14["agents"][0]
        self.assertTrue(day14["stale"])
        self.assertEqual(row["last_seen_running_on"], "2026-10-01", "no liveness date after the scan")
        self.assertEqual(row["first_seen_on"], "2026-10-01")
        self.assertTrue(any("2026-10-01" in d["reason"] for d in day14["degraded"]))
        day15 = document(scan, mcp_scan(), day14, "2026-10-15",
                         now=datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
        self.assertEqual({k: v for k, v in day15.items() if k != "updated_on"},
                         {k: v for k, v in day14.items() if k != "updated_on"},
                         "the same old scan a day later changes nothing but the stamp the writer carries")

    def test_a_scan_from_this_tick_is_fresh(self):
        self.put(descriptor("alpha-agent", 47401))
        self.assertFalse(document(self.scan(launchd={}), mcp_scan(), None)["stale"])

    # --- L1: reads are bounded and never block ---------------------------------------------
    def test_a_link_outside_the_services_directory_is_not_followed(self):
        outside = self.base / "elsewhere.json"
        outside.write_text(json.dumps(descriptor("alpha-agent", 47401)), encoding="utf-8")
        (self.services / "alpha-agent.default.json").symlink_to(outside)
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertEqual(doc["agents"], [])
        self.assertTrue(any("not followed" in d["reason"] for d in doc["degraded"]))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs here")
    def test_a_fifo_named_like_a_descriptor_is_refused_without_blocking(self):
        os.mkfifo(self.services / "pipe.default.json")
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertTrue(any("not a regular file" in d["reason"] for d in doc["degraded"]))

    def test_an_oversized_or_too_deep_descriptor_is_refused_and_the_others_listed(self):
        self.put(descriptor("alpha-agent", 47401))
        (self.services / "huge.default.json").write_text(
            json.dumps({"x": "a" * (scan_agents.MAX_DESCRIPTOR_BYTES + 1)}), encoding="utf-8")
        (self.services / "deep.default.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertEqual([r["service_id"] for r in doc["agents"]], ["alpha-agent"])
        reasons = " ".join(d["reason"] for d in doc["degraded"])
        self.assertIn("huge.default.json: larger than", reasons)
        self.assertIn("deep.default.json", reasons)

    def test_string_fields_are_bounded(self):
        doc = descriptor("alpha-agent", 47401)
        doc["summary"] = "s" * 100_000
        doc["name"] = "n" * 10_000
        self.put(doc)
        row = document(self.scan(launchd={}), mcp_scan(), None)["agents"][0]
        self.assertLessEqual(len(row["summary"]), 200)
        self.assertLessEqual(len(row["name"]), 80)

    # --- L2: names and text that are not UTF-8 --------------------------------------------
    def test_a_lone_surrogate_in_a_descriptor_is_written_as_utf8(self):
        self.put(descriptor("alpha-agent", 47401))
        path = self.services / "alpha-agent.default.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"A synthetic agent."', '"bad \\ud800"'),
                        encoding="utf-8")
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        json.dumps(doc, ensure_ascii=False).encode("utf-8")        # what atomic.write_json does

    def test_a_file_name_that_is_not_utf8_is_named_and_skipped(self):
        try:
            fd = os.open(os.fsencode(self.services) + b"/bad-\xff.json", os.O_CREAT | os.O_WRONLY, 0o600)
        except (OSError, ValueError):
            self.skipTest("this file system refuses names that are not UTF-8")
        os.close(fd)
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertTrue(any("not UTF-8" in d["reason"] for d in doc["degraded"]))
        json.dumps(doc, ensure_ascii=False).encode("utf-8")

    # --- L3: one origin, two descriptors ---------------------------------------------------
    def test_two_descriptors_on_one_origin_attribute_to_neither_and_say_so(self):
        self.put(descriptor("alpha-agent", 47401))
        self.put(descriptor("beta-agent", 47401))
        doc = document(self.scan(launchd={}), mcp_scan(mcp_server("x", "http://127.0.0.1:47401/mcp")), None)
        self.assertEqual([r["mcp_declarations"] for r in doc["agents"]], [[], []])
        self.assertEqual(doc["totals"]["mcp"]["ambiguous"], 1)
        self.assertTrue(any(d["source"] == "mcp-inventory" and "beta-agent" in d["reason"]
                            for d in doc["degraded"]))

    def test_an_uppercase_remote_host_still_joins(self):
        remote = descriptor("beta-agent", remote="https://beta.example.com")
        remote["origin"] = "https://Beta.Example.COM"
        self.put(remote)
        doc = document(self.scan(launchd={}), mcp_scan(mcp_server("b", "https://beta.example.com/mcp")), None)
        self.assertEqual(doc["agents"][0]["host"], "beta.example.com")
        self.assertEqual(doc["agents"][0]["mcp_declarations"], ["mcp:claude/user/b"])

    # --- L4: carried rows ----------------------------------------------------------------------
    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "permission bits do not bind here")
    def test_carried_rows_say_since_when_and_are_not_counted_as_running(self):
        previous = document(self.two(), mcp_scan(), None)
        self.assertEqual(previous["totals"]["by_supervisor_state"].get("running"), 1)
        self.services.chmod(0)
        try:
            doc = document(self.scan(launchd={}), mcp_scan(), previous)
        finally:
            self.services.chmod(0o700)
        self.assertTrue(all(r["carried_since"] == TODAY for r in doc["agents"]))
        self.assertEqual(doc["totals"]["by_supervisor_state"], {})
        self.assertEqual(doc["totals"]["carried"], 2)

    # --- L5: the collector never writes what its schema refuses ---------------------------------
    def test_a_hostile_descriptor_still_yields_a_schema_valid_document(self):
        try:
            import jsonschema
        except ImportError:                                             # pragma: no cover
            self.skipTest("jsonschema is part of the full extra")
        bad = descriptor("alpha-agent", 47401)
        bad.update(id="Not An Id", instance="X", origin="http://127.0.0.1:70000", name=["a list"],
                   fabricManifest=self.put_manifest("alpha-agent"))
        self.put(bad, "hostile.default.json")
        manifest = self.home / "fixture/alpha-agent/fabric-agent.json"
        m = json.loads(manifest.read_text(encoding="utf-8"))
        m["contractVersion"] = 5
        m["provider"]["revision"] = "seven"
        m["provider"]["supportedContractVersions"] = "0.1.0"
        manifest.write_text(json.dumps(m), encoding="utf-8")
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(doc)
        row = doc["agents"][0]
        self.assertEqual(row["id"], "agent:file:hostile.default")
        self.assertIsNone(row["port"])
        self.assertFalse(row["valid"])
        self.assertEqual(scan_agents.problems(doc), [])

    def test_problems_checks_the_id_the_port_and_the_manifest_types(self):
        self.put(descriptor("alpha-agent", 47401, manifest=self.put_manifest("alpha-agent")))
        doc = document(self.scan(launchd={}), mcp_scan(), None)
        for field, value, words in (("id", "agent:Bad Id", "agent id"), ("port", 70000, "port"),
                                    ("manifest", {"state": "pinned", "provider_revision": "x"},
                                     "manifest.provider_revision")):
            bad = json.loads(json.dumps(doc))
            bad["agents"][0][field] = value
            self.assertTrue(any(words in p for p in scan_agents.problems(bad)), field)

    # --- LOW: home paths, absolute and Windows ---------------------------------------------------
    def test_absolute_home_paths_are_written_with_a_tilde(self):
        manifest = self.put_manifest("alpha-agent").replace("~", str(self.home), 1)
        doc = descriptor("alpha-agent", 47401, manifest=manifest)
        doc["lifecycle"]["plist"] = str(self.home / "Library/LaunchAgents/com.example.alpha-agent.plist")
        self.put(doc)
        out = document(self.scan(launchd={}), mcp_scan(), None)
        row = out["agents"][0]
        self.assertEqual(row["manifest"]["path"], "~/fixture/alpha-agent/fabric-agent.json")
        self.assertEqual(row["supervisor"]["plist"], "~/Library/LaunchAgents/com.example.alpha-agent.plist")
        self.assertNotIn(str(self.home), json.dumps(out))

    def test_a_windows_home_is_written_with_a_tilde_and_refused_when_absolute(self):
        with patch.object(scan_agents, "_home", return_value="C:\\Users\\alpha"):
            self.assertEqual(scan_agents.tilde("C:\\Users\\alpha\\fixture\\m.json"), "~\\fixture\\m.json")
            self.put(descriptor("alpha-agent", 47401))
            doc = document(self.scan(launchd={}), mcp_scan(), None)
            doc["agents"][0]["descriptor"] = "C:\\Users\\alpha\\x.json"
            self.assertTrue(any("absolute home path" in p for p in scan_agents.problems(doc)))

    # --- LOW: the validator's agents block -----------------------------------------------------
    def test_validate_registry_refuses_a_broken_agents_document(self):
        import validator_fixture
        root = self.base / "validator"
        root.mkdir()
        reg = validator_fixture.seed(root)
        sources = json.loads((reg / "sources.json").read_text(encoding="utf-8"))
        sources["sources"] += [{"id": "SRC-0020"}, {"id": "SRC-0017"}]
        (reg / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        self.put(descriptor("alpha-agent", 47401))
        good = document(self.scan(launchd={}), mcp_scan(), None)

        def validate(doc):
            (reg / "agents.json").write_text(json.dumps(doc), encoding="utf-8")
            env = {**validator_fixture.environment(reg), "HOME": str(self.home), "USERPROFILE": str(self.home)}
            return subprocess.run([sys.executable, "tools/validate_registry.py"], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=120)
        p = validate(good)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-400:])
        bad = json.loads(json.dumps(good))
        bad["agents"].append(bad["agents"][0])
        bad["totals"]["agents"] = 2
        p = validate(bad)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("agents.json: duplicate agent ids", p.stderr)

    # --- LOW: the tick's switch and the CLI group -----------------------------------------------
    def test_scan_agents_runs_only_with_machine_watch(self):
        import tick_lease
        self.assertEqual(tick_lease.FEATURE_STEPS.get("scan-agents"), "machine_watch")
        with patch.object(tick_lease.configuration, "enabled", side_effect=lambda name, kind="integrations":
                          not (kind == "features" and name == "machine_watch")):
            self.assertFalse(tick_lease.step_allowed("scan-agents"))
        with patch.object(tick_lease.configuration, "enabled", return_value=True):
            self.assertTrue(tick_lease.step_allowed("scan-agents"))

    def test_scan_agents_is_in_the_cli_all_group(self):
        import ast
        tree = ast.parse((ROOT / "observatory.py").read_text(encoding="utf-8"))
        groups = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "GROUPS" for t in n.targets))
        everything = next(v for k, v in zip(groups.keys, groups.values)
                          if isinstance(k, ast.Constant) and k.value == "all")
        order = ast.literal_eval(everything)
        self.assertIn("scan-agents", order)
        self.assertLess(order.index("scan-mcp"), order.index("emit"))
        self.assertLess(order.index("scan-agents"), order.index("emit"))


def no_slash(form: str, secret: str, rest: str = "h.example.com/x") -> str:
    """A credential URL without the usual `//`, assembled at run time like `keyed()`."""
    return form + "u:" + secret + chr(64) + rest


class FixCycle2(Fixture):
    """Review round 3 of #288: each case fails against 03ca5722."""

    # --- MEDIUM 1: one harmless query URL halted the tick --------------------------------
    def test_every_emitted_string_is_sanitised_and_problems_stays_empty(self):
        secrets = [f"SECRETX{i}" for i in range(9)]
        doc = descriptor("alpha-agent", 47401)
        doc["summary"] = "Guide: https://example.com/guide?lang=en and " + keyed(secrets[0], "docs.example.com/a")
        doc["name"] = "Alpha " + keyed(secrets[1], "n.example.com")
        doc["lifecycle"]["label"] = keyed(secrets[2], "l.example.com") + "?q=" + secrets[3]
        doc["lifecycle"]["plist"] = keyed(secrets[4], "p.example.com/x.plist")
        doc["fabricManifest"] = keyed(secrets[5], "m.example.com/fabric-agent.json")
        self.put(doc)
        beta = descriptor("beta-agent", 47402, manager="systemd", manifest=self.put_manifest("beta-agent"))
        beta["lifecycle"] = {"manager": keyed(secrets[6], "mgr.example.com") + "?k=" + secrets[7]}
        self.put(beta)
        manifest = self.home / "fixture/beta-agent/fabric-agent.json"
        m = json.loads(manifest.read_text(encoding="utf-8"))
        m["provider"]["id"] = keyed(secrets[8], "pid.example.com") + "?x=1"
        manifest.write_text(json.dumps(m), encoding="utf-8")
        out = document(self.scan(launchd={}), mcp_scan(), None)
        text = json.dumps(out)
        for secret in secrets:
            self.assertEqual(text.count(secret), 0, secret)
        self.assertNotIn("lang=en", text)
        self.assertIn("Guide: [url]", text, "a URL with a query is replaced whole, never half-cleaned")
        self.assertEqual(scan_agents.problems(out), [])

    def test_a_scan_written_by_an_older_collector_is_sanitised_by_the_emit(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd={})
        scan["services"][0]["summary"] = "see " + keyed("SECRETOLD", "old.example.com") + " and " + str(self.home)
        out = document(scan, mcp_scan(), None)
        self.assertNotIn("SECRETOLD", json.dumps(out))
        self.assertEqual(scan_agents.problems(out), [])

    # --- MEDIUM 2: URLs without the usual `//` ------------------------------------------
    def test_credential_urls_without_two_slashes_lose_their_credentials(self):
        forms = {"https:": "SECRETF1", "http:/": "SECRETF2", "http:\\\\": "SECRETF3"}
        for form, secret in forms.items():
            with self.subTest(form=form):
                for p in self.services.glob("*.json"):
                    p.unlink()
                doc = descriptor("alpha-agent", 47401)
                doc["source"] = {"repository": no_slash(form, secret)}
                self.put(doc)
                out = document(self.scan(launchd={}), mcp_scan(), None)
                self.assertEqual(json.dumps(out).count(secret), 0)
                self.assertTrue(out["agents"][0]["key_in_url"])
                self.assertEqual(scan_agents.problems(out), [])

    def test_problems_refuses_credential_urls_without_two_slashes(self):
        self.put(descriptor("alpha-agent", 47401))
        good = document(self.scan(launchd={}), mcp_scan(), None)
        for form in ("https:", "http:/", "http:\\\\"):
            bad = json.loads(json.dumps(good))
            bad["agents"][0]["summary"] = no_slash(form, "pw")
            self.assertTrue(any("user-info or a query" in p for p in scan_agents.problems(bad)), form)

    # --- LOW --------------------------------------------------------------------------------
    def test_a_home_path_inside_a_string_is_written_with_a_tilde_and_refused_raw(self):
        doc = descriptor("alpha-agent", 47401)
        doc["summary"] = f"notes in {self.home}/fixture/notes.md for the team"
        doc["source"] = {"repository": "file://" + str(self.home) + "/repo"}
        self.put(doc)
        out = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertNotIn(str(self.home), json.dumps(out))
        self.assertIn("~/fixture/notes.md", out["agents"][0]["summary"])
        self.assertIsNone(out["agents"][0]["source_repository"], "file: is not a repository scheme kept")
        bad = json.loads(json.dumps(out))
        bad["agents"][0]["summary"] = f"inside {self.home}/x here"
        self.assertTrue(any("absolute home path" in p for p in scan_agents.problems(bad)))

    def test_url_params_are_dropped(self):
        doc = descriptor("alpha-agent", 47401)
        doc["source"] = {"repository": "https://git.example.com/x;jsessionid=SECRETP1/y"}
        self.put(doc)
        out = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertNotIn("SECRETP1", json.dumps(out))
        self.assertIsNone(out["agents"][0]["source_repository"], "a ;param is refused whole")

    def test_an_unknown_command_key_is_not_echoed(self):
        doc = descriptor("alpha-agent", 47401)
        doc["commands"]["SECRETCMD9"] = ["~/x"]
        self.put(doc)
        out = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertNotIn("SECRETCMD9", json.dumps(out))
        self.assertTrue(any(p.startswith("unknown command") for p in out["agents"][0]["problems"]))

    def test_stdio_is_counted_by_transport_and_the_rest_is_other(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = mcp_scan(mcp_server("tool", None, command="alpha-tool"),
                        {"name": "sock", "agent": "claude", "scope": "user", "transport": "http",
                         "target": "ws://h.example.com/x", "liveness": "not-probed"},
                        {"name": "broken", "agent": "claude", "scope": "user", "transport": "http",
                         "target": "http://[bad", "liveness": "not-probed"})
        totals = document(self.scan(launchd={}), scan, None)["totals"]["mcp"]
        self.assertEqual((totals["stdio"], totals["other"], totals["unattributed_http"]), (1, 2, 0))

    def test_a_scan_dated_in_the_future_is_stale_with_clock_skew(self):
        self.put(descriptor("alpha-agent", 47401))
        future = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
        out = document(self.scan(launchd={}, now=future), mcp_scan(), None, now=NOW)
        self.assertTrue(out["stale"])
        self.assertTrue(any(d["reason"].startswith("clock-skew") for d in out["degraded"]))

    def test_the_duplicate_check_is_linear(self):
        import time
        rows = [{"id": f"agent:a{i}.default", "supervisor": {"state": "running"}, "manifest": {"state": "absent"}}
                for i in range(30_000)]
        rows.append(dict(rows[0]))
        doc = {"agents": rows, "totals": {"agents": len(rows)}, "source_refs": ["SRC-0020"]}
        start = time.process_time()                  # CPU time: a loaded machine does not count
        found = scan_agents.problems(doc)
        self.assertLess(time.process_time() - start, 10.0, "a quadratic count takes minutes here")
        self.assertTrue(any("duplicate" in p for p in found))


class Round4(Fixture):
    """Review round 4 of #288: each half of a two-part guard has a test that fails without it."""

    def test_a_quoted_value_in_a_problem_is_dropped_even_without_a_url(self):
        doc = descriptor("alpha-agent", 47401)
        doc["paths"]["data"] = "\\\\fileserver\\share\\SECRETQ1"       # a network share, quoted back by %r
        self.put(doc)
        out = document(self.scan(launchd={}), mcp_scan(), None)
        self.assertNotIn("SECRETQ1", json.dumps(out))
        self.assertTrue(any("network share" in p for p in out["agents"][0]["problems"]))

    def test_an_unquoted_url_in_a_problem_is_made_safe(self):
        shown = scan_agents._safe_problem("the origin " + keyed("SECRETQ2", "o.example.com") + " is refused")
        self.assertNotIn("SECRETQ2", shown)
        self.assertIn("[url]", shown)

    def test_a_valid_id_with_a_bad_instance_is_a_file_row(self):
        doc = descriptor("alpha-agent", 47401, instance="Bad Instance")
        self.put(doc, "alpha.json")
        row = document(self.scan(launchd={}), mcp_scan(), None)["agents"][0]
        self.assertEqual((row["id"], row["service_id"], row["instance"]), ("agent:file:alpha", "alpha-agent", None))

    def test_a_bad_id_with_a_valid_instance_is_a_file_row(self):
        doc = descriptor("alpha-agent", 47401)
        doc["id"] = "Alpha Agent"
        self.put(doc, "alpha.json")
        row = document(self.scan(launchd={}), mcp_scan(), None)["agents"][0]
        self.assertEqual((row["id"], row["service_id"], row["instance"]), ("agent:file:alpha", None, "default"))

    def test_tilde_handles_each_separator_on_its_own(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            self.assertEqual(scan_agents.tilde("at /srv/alpha/x and /srv/alpha"), "at ~/x and ~")
            self.assertEqual(scan_agents.tilde("/srv/alphabet/x"), "/srv/alphabet/x", "a longer name is not home")
        with patch.object(scan_agents, "_home", return_value="C:\\Users\\alpha"):
            self.assertEqual(scan_agents.tilde("in C:\\Users\\alpha\\x"), "in ~\\x")

    def test_port_zero_and_a_port_past_65535_are_no_port_and_not_a_key(self):
        for origin in ("http://127.0.0.1:0", "http://127.0.0.1:70000"):
            with self.subTest(origin=origin):
                for p in self.services.glob("*.json"):
                    p.unlink()
                doc = descriptor("alpha-agent", 47401)
                doc["origin"] = origin
                self.put(doc)
                row = document(self.scan(launchd={}), mcp_scan(), None)["agents"][0]
                self.assertIsNone(row["port"])
                self.assertFalse(row["key_in_url"])
                self.assertIsNone(row["origin"], "an unusable port refuses the whole origin")


AT = chr(64)        # `@`, kept out of the source so the public-release gate never sees a credential URL


def tiny_doc(*strings: str) -> dict:
    """The smallest document `problems()` reads, carrying `strings` as free text."""
    return {"agents": [], "removed": [], "totals": {"agents": 0}, "source_refs": ["SRC-0020"],
            "note": list(strings)}


class FixCycle3(Fixture):
    """Review round 5 of #288, the fail-closed redesign: whole tokens are kept or replaced."""

    # --- every PoC of rounds 3 and 5 ---------------------------------------------------------
    POCS = [
        "Docs:" + keyed("hunter2", "example.com/x"),                   # F1 glued scheme
        "http:///tok:SECRETA" + AT + "127.0.0.1:9002/",                 # F2 three slashes
        "see https:/example.com?token=SECRETB",                          # F3 one slash and a query
        "https:example.com?token=SECRETC", "https:example.com#frag=SECRETD",
        "note:a" + AT + "/b" + AT + "/c" + AT + "d", "ops:x" + AT + "/y" + AT + "/z" + AT + "/w" + AT + "v",  # F4
        "pt:s" + AT + "/SECRETE" + AT + "%", "http:a" + AT + "/b" + AT + "c", "git:git" + AT + "/srv/repo" + AT + "main",
        "key:" + keyed("SECRETF", "example.com/x"), " -" + keyed("SECRETG", "h/"), " .http://u:SECRETH" + AT + "h/",
        "https:////git:SECRETI" + AT + "github.com/o/r", "http:\\\\\\u:SECRETJ" + AT + "h/",
        "https:\\/\\/u:SECRETK" + AT + "h\\/x", "HtTp://U:SECRETL" + AT + "H/", "http://u%40x:SECRETM" + AT + "h/",
        "http://[fe80::1%25en0]:8080/?SECRETN=1", "ws://u:SECRETO" + AT + "h/?k=1", "file://u:SECRETP" + AT + "h/x",
        "javascript:fetch('//u:SECRETQ" + AT + "h')", "data:text/plain;base64,u:SECRETR" + AT + "h",
        "Guide: https://example.com/guide?lang=SECRETS",
    ]

    def test_every_poc_is_replaced_whole_idempotent_and_clean(self):
        for poc in self.POCS:
            with self.subTest(poc=poc):
                once = scan_agents._text(poc)
                self.assertEqual(scan_agents._text(once), once, "idempotent")
                self.assertEqual(scan_agents.problems(tiny_doc(once)), [])
                for marker in ("SECRET", "hunter2"):
                    self.assertNotIn(marker, once)
                self.assertNotIn(AT, once)

    def test_strict_urls_are_kept_and_normalised(self):
        for given, kept in (("https://Example.COM/a/b", "https://example.com/a/b"),
                            ("http://127.0.0.1:47401", "http://127.0.0.1:47401"),
                            ("see wss://h.example.com/x now", "see wss://h.example.com/x now")):
            self.assertEqual(scan_agents._text(given), kept)

    def test_the_round5_pipeline_leaks_nothing(self):
        """pipe.py of round 5: five descriptors, every secret through the whole emit path."""
        def put(i, **kw):
            doc = descriptor(f"svc-{i}", 9000 + i, manager="none")
            doc.update(kw)
            self.put(doc)
        put(1, summary="Docs:" + keyed("hunter2", "example.com/x"))
        put(2, origin="http:///tok:SECRET1" + AT + "127.0.0.1:9002/")
        put(3, summary="see https:/example.com?token=SECRET2")
        put(4, source={"repository": "https:////git:SECRET3" + AT + "github.com/o/r"})
        put(5, origin="key:" + keyed("SECRET4", "127.0.0.1:9005"))
        out = document(self.scan(launchd={}), mcp_scan(), None)
        text = json.dumps(out)
        for marker in ("hunter2", "SECRET"):
            self.assertNotIn(marker, text)
        rows = {r["service_id"]: r for r in out["agents"]}
        for sid in ("svc-2", "svc-5"):
            self.assertIsNone(rows[sid]["origin"])
            self.assertIsNone(rows[sid]["host"])
            self.assertTrue(rows[sid]["key_in_url"])
        self.assertTrue(rows["svc-4"]["key_in_url"])
        self.assertEqual(sum(1 for d in out["degraded"] if d["reason"].startswith("origin unreadable")), 2)
        self.assertEqual(scan_agents.problems(out), [])

    # --- the property: 100 000 strings ------------------------------------------------------
    def test_fuzz_idempotent_clean_and_no_marker_survives(self):
        import random
        rng = random.Random(288)
        weighted = list(":/\\@?#%") * 4 + list("abhtpsxuS.-=~(') ") + ["\n", "é", "…", "/srv/alpha"]
        junk = [c for c in weighted if not c.isspace()]
        pieces = ["u:Zq9K" + AT + "h", "x:?Zq9K", "x:#Zq9K", "Zq9K" + AT, "http://h/?Zq9K",
                  "ab:/Zq9K", "ab:\\Zq9K", "https:Zq9K" + AT + "h", "http:\\\\Zq9K" + AT + "h"]
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            for i in range(100_000):
                text = "".join(rng.choice(weighted) for _ in range(rng.randint(0, 24)))
                if i % 2:
                    piece = ("".join(rng.choice(junk) for _ in range(rng.randint(0, 4))) + rng.choice(pieces)
                             + "".join(rng.choice(junk) for _ in range(rng.randint(0, 4))))
                    text = text + rng.choice(" \n") + piece + rng.choice(" \n") + text[::-1]
                once = scan_agents._text(text, rng.choice(("name", "summary", "default")))
                if scan_agents._text(once) != once:
                    self.fail(f"not idempotent: {text!r} -> {once!r}")
                if "Zq9K" in once:
                    self.fail(f"marker survived: {text!r} -> {once!r}")
                found = scan_agents.problems(tiny_doc(once))
                home_only = found == ["agents.json holds an absolute home path; paths under home are written ~/"]
                # The one refusal sanitised text may still draw: a home the sanitiser leaves because
                # a letter, digit, `~` or `:` precedes it (past separators and dots). `home_leaks`
                # refuses it on purpose (fail closed).
                if found and not (home_only and re.search(r"(?:[^\W_]|[~:])[/\\.]*/srv/alpha", once)):
                    self.fail(f"problems() on sanitised text: {text!r} -> {once!r}: {found}")

    def test_a_keyed_token_cut_by_the_first_bound_leaves_no_piece(self):
        text = "a " * 700 + "u:SECRETLONG" + AT + "h" * 50
        self.assertNotIn("SECRET", scan_agents._text(text, "name"))
        self.assertEqual(scan_agents._text("b" * 1000 + "u:SECRET" + AT + "h", "name"), "…")

    # --- F5: no regex backtracks over unbounded input --------------------------------------------
    def test_a_200kb_pathological_command_key_collects_in_under_a_second(self):
        import time
        doc = descriptor("alpha-agent", 47401)
        doc["commands"] = {"'" + "\\'" * 66_000: ["/bin/true"]}
        self.put(doc)
        self.assertGreater((self.services / "alpha-agent.default.json").stat().st_size, 190_000)
        start = time.process_time()
        scan = self.scan(launchd={})
        self.assertLess(time.process_time() - start, 1.0)
        self.assertIn("unknown command …", scan["services"][0]["problems"])

    def test_pathological_free_text_is_linear(self):
        import time
        for text in ("a" * 250_000, "ab:" * 80_000, "ab:" + "x" + AT * 120_000, "'" + "\\'" * 120_000,
                     "http://[" + ":" * 250_000, "-ab:" * 60_000):
            start = time.process_time()
            scan_agents._text(text)
            scan_agents._safe_problem(text)
            self.assertLess(time.process_time() - start, 1.0, text[:12])

    # --- F8: rewritten file names keep distinct ids ------------------------------------------------
    def test_rewritten_file_names_get_a_stable_hash(self):
        for name in ("my agent.json", "my-agent.json", "my+agent.json"):
            (self.services / name).write_text(json.dumps({"id": "BAD ID", "name": name}), encoding="utf-8")
        ids = [r["id"] for r in self.scan(launchd={}, platform="linux")["services"]]
        self.assertEqual(len(set(ids)), 3, ids)
        self.assertIn("agent:file:my-agent", ids)
        self.assertEqual(ids, [r["id"] for r in self.scan(launchd={}, platform="linux")["services"]], "stable")
        self.assertTrue(all(scan_agents.AGENT_ID.match(i) for i in ids))

    # --- F9: shapes an older collector or a hand edit could leave ---------------------------------
    def test_a_scan_that_is_not_an_object_is_refused(self):
        for scan in (["not", "a", "scan"], {"services": "x"}, {"services": [], "sources": []}):
            with self.assertRaises(TypeError):
                scan_agents.document(scan, None, None, TODAY, now=NOW)

    def test_a_previous_document_that_is_not_an_object_restarts_dates_and_says_so(self):
        self.put(descriptor("alpha-agent", 47401))
        out = document(self.scan(launchd={}), mcp_scan(), ["junk"])
        self.assertEqual(out["agents"][0]["first_seen_on"], TODAY)
        self.assertTrue(any("previous agents document" in d["reason"] for d in out["degraded"]))

    def test_junk_scan_fields_are_dropped_or_validated(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd={})
        for junk in ({"id": "not an id"}, "not a row", {}):
            bad_scan = json.loads(json.dumps(scan))
            bad_scan["services"].append(junk)
            with self.subTest(row=junk), self.assertRaises(ValueError):
                document(bad_scan, mcp_scan(), None)
        scan["degraded"].append({"source": "elsewhere", "reason": 5, "agent": "x"})
        scan["scanned_at"] = "garbage" + scan["scanned_at"]
        previous = {"agents": [{"id": "../../etc"}, {"id": "agent:gone-agent.default"}]}
        out = document(scan, mcp_scan(), previous)
        self.assertEqual([r["id"] for r in out["agents"]], ["agent:alpha-agent.default"])
        self.assertEqual(out["removed"], ["agent:gone-agent.default"])
        self.assertIsNone(out["scanned_on"])
        self.assertTrue(out["stale"])
        self.assertEqual(scan_agents.problems(out), [])
        self.assertTrue(all(d["source"] in scan_agents.DEGRADED_SOURCES and isinstance(d["reason"], str)
                            for d in out["degraded"]))
        bad = json.loads(json.dumps(out))
        bad["degraded"].append({"source": "elsewhere", "reason": 5})
        self.assertTrue(any("degraded" in p for p in scan_agents.problems(bad)))
        bad = json.loads(json.dumps(out))
        bad["scanned_on"], bad["removed"] = "yesterday", ["x"]
        found = scan_agents.problems(bad)
        self.assertTrue(any("scanned_on" in p for p in found) and any("removed" in p for p in found))

    # --- F7: the older-collector path and the remaining shapes ----------------------------------
    def test_keyed_urls_in_scan_degraded_and_sources_are_replaced(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd={})
        scan["degraded"].append({"source": "fabric-services", "reason": "seen at " + keyed("SECRETDG", "h.example.com")})
        scan["sources"]["fabric_services"]["directory"] = keyed("SECRETSRC", "h.example.com/dir")
        out = document(scan, mcp_scan(), None)
        text = json.dumps(out)
        self.assertNotIn("SECRETDG", text)
        self.assertNotIn("SECRETSRC", text)
        self.assertEqual(scan_agents.problems(out), [])

    def test_a_url_the_final_cut_halves_is_replaced_not_left_half(self):
        text = "a" * 72 + " http://h.example.com/x"
        once = scan_agents._text(text, "name")
        self.assertLessEqual(len(once), 80)
        self.assertEqual(scan_agents._text(once), once, "idempotent")
        self.assertNotIn("http:/", once)
        self.assertEqual(scan_agents.problems(tiny_doc(once)), [])

    def test_a_fragment_and_a_backslash_rendering_are_replaced(self):
        self.assertEqual(scan_agents._text("at https://h.example.com/x#SECRETFR"), "at [url]")
        self.assertEqual(scan_agents._text("https:\\/\\/u:SECRETBS" + AT + "h\\/x"), "[url]")

    def test_a_refused_origin_carries_nothing_and_cannot_join(self):
        doc = descriptor("alpha-agent", 47401)
        doc["origin"] = "http://127.0.0.1:47401/?token=SECRETQO"
        self.put(doc)
        out = document(self.scan(launchd={}), mcp_scan(mcp_server("a", "http://127.0.0.1:47401/mcp")), None)
        row = out["agents"][0]
        self.assertEqual((row["origin"], row["host"], row["port"], row["key_in_url"]), (None, None, None, True))
        self.assertEqual(row["mcp_declarations"], [], "no join without a strict origin")
        self.assertNotIn("SECRETQO", json.dumps(out))

    # --- the home path: boundaries, separators, case -----------------------------------------------
    def test_home_needs_a_left_boundary_and_a_right_one(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            self.assertEqual(scan_agents._text("HOME=/srv/alpha/x (/srv/alpha) '/srv/alpha'"), "HOME=~/x (~) '~'")
            self.assertEqual(scan_agents._text("abc/srv/alpha/x"), "abc/srv/alpha/x", "no left boundary")
            self.assertEqual(scan_agents._text("/srv/alpha.bak"), "/srv/alpha.bak", "a name character follows")
            self.assertEqual(scan_agents.problems(tiny_doc("/srv/alpha.bak")), [])
            self.assertTrue(scan_agents.problems(tiny_doc("abc/srv/alpha/x")),
                            "another path ending in the home is refused, never written (fail closed)")

    def test_home_is_case_insensitive_only_where_the_file_system_is(self):
        with patch.object(scan_agents, "_home", return_value="/srv/Alpha"):
            with patch.object(scan_agents.sys, "platform", "darwin"):
                self.assertEqual(scan_agents._text("/SRV/alpha/x"), "~/x")
            with patch.object(scan_agents.sys, "platform", "linux"):
                self.assertEqual(scan_agents._text("/SRV/alpha/x"), "/SRV/alpha/x")

    def test_a_home_inside_a_file_url_is_replaced_with_the_whole_token(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            self.assertEqual(scan_agents._text("repo file:///srv/alpha/r"), "repo [url]")

    # --- problems() is the sanitiser's own rule -------------------------------------------------
    def test_problems_fires_only_on_text_the_sanitiser_never_touched(self):
        for raw in self.POCS + ["plain", "/srv/x", "C:\\work\\x"]:
            self.assertEqual(scan_agents.problems(tiny_doc(scan_agents._text(raw))), [], raw)
        self.assertTrue(scan_agents.problems(tiny_doc("see https:example.com?k=1")))


class FixCycle4(Fixture):
    """Review round 6 of #288."""

    def test_a_file_name_shaped_like_a_url_is_not_read_and_not_named(self):
        self.put(descriptor("alpha-agent", 47401))
        (self.services / ("https:u:SECRETFN" + AT + "h.json")).write_text(
            json.dumps(descriptor("beta-agent", 47402)), encoding="utf-8")
        (self.services / "x:?SECRETFQ.json").write_text("{}", encoding="utf-8")
        out = document(self.scan(launchd={}), mcp_scan(), None)
        text = json.dumps(out)
        self.assertNotIn("SECRETF", text)
        self.assertEqual([r["id"] for r in out["agents"]], ["agent:alpha-agent.default"])
        self.assertEqual(sum(1 for d in out["degraded"] if d["reason"] == "a file name shaped like a URL; not read"), 2)
        self.assertEqual(scan_agents.problems(out), [])

    def test_unusable_rewrites_the_previous_document_once(self):
        self.put(descriptor("alpha-agent", 47401))
        prev = document(self.scan(launchd={}), mcp_scan(), None)
        once = scan_agents.unusable(prev, "TypeError", "2026-10-15")
        twice = scan_agents.unusable(once, "TypeError", "2026-10-15")
        self.assertTrue(once["stale"])
        self.assertEqual(once, twice, "rewriting it again changes nothing")
        self.assertIsNone(scan_agents.unusable(["junk"], "TypeError", "2026-10-15"))

    def test_only_the_collectors_own_sources_are_copied(self):
        self.put(descriptor("alpha-agent", 47401))
        scan = self.scan(launchd={})
        scan["sources"][keyed("SECRETSK", "h")] = {"state": "read"}
        out = document(scan, mcp_scan(), None)
        self.assertEqual(sorted(out["sources"]), ["fabric_services", "launchd", "mcp_inventory"])
        self.assertNotIn("SECRETSK", json.dumps(out))

    def test_dict_keys_are_sanitised(self):
        self.assertNotIn("SECRETDK", json.dumps(scan_agents._sanitise({keyed("SECRETDK", "h"): {"x": 1}})))

    def test_an_nfd_home_is_still_home(self):
        with patch.object(scan_agents, "_home", return_value="/srv/café"):
            self.assertEqual(scan_agents._text("at /srv/café/x"), "at ~/x")

    def test_unicode_separators_split_tokens(self):
        for sep in (" ", " ", "　"):
            with self.subTest(sep=hex(ord(sep))):
                text = "note" + sep + "https://h.example.com/x"
                self.assertEqual(scan_agents._text(text), text, "a strict URL beside a word is kept")

    def test_an_at_in_a_strict_path_is_refused(self):
        self.assertEqual(scan_agents._text("https://h.example.com/a" + AT + "b"), "[url]")
        self.assertIsNone(scan_agents.strict_url("https://h.example.com/a" + AT + "b"))

    def test_the_strict_host_starts_alphanumeric_and_a_fragment_is_keyed(self):
        self.assertIsNone(scan_agents.strict_url("https://-h.example.com"))
        self.assertIsNone(scan_agents.strict_url("https://.h.example.com"))
        doc = descriptor("alpha-agent", 47401)
        doc["origin"] = "https://h.example.com#SECRETHF"
        self.put(doc)
        row = document(self.scan(launchd={}), mcp_scan(), None)["agents"][0]
        self.assertTrue(row["key_in_url"])
        self.assertIsNone(row["origin"])


class FixCycle5(Fixture):
    """Review round 7 of #288."""

    def test_a_scan_without_services_or_its_source_is_refused(self):
        now = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
        for scan in ({"schema_version": 1, "scanned_at": now},
                     {"scanned_at": now, "services": []},
                     {"scanned_at": now, "services": [], "sources": {}},
                     {"scanned_at": now, "services": [], "sources": {"fabric_services": {"state": "guessed"}}}):
            with self.subTest(scan=sorted(scan)), self.assertRaises(TypeError):
                scan_agents.document(scan, None, None, TODAY, now=NOW)

    def test_an_empty_scan_through_the_emit_keeps_the_previous_rows_stale(self):
        import workspace
        from emitter_fixture import environment, seed
        root = self.base / "estate"
        root.mkdir()
        seed(root)
        workspace.initialize(root / "home", identities=False)
        raw, reg = root / "raw", root / "registry"
        env = {**environment(root), "OBSERVATORY_HOME": str(root / "home")}
        now = datetime.now(timezone.utc)

        def emit():
            p = subprocess.run([sys.executable, "collectors/emit_registry.py", str(raw)], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr[-400:])
            return json.loads((reg / "agents.json").read_text(encoding="utf-8"))
        self.put(descriptor("alpha-agent", 47401))
        (raw / "agents.json").write_text(json.dumps(self.scan(launchd={}, now=now)), encoding="utf-8")
        good = emit()
        (raw / "agents.json").write_text(json.dumps({"schema_version": 1,
                                                     "scanned_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}),
                                         encoding="utf-8")
        after = emit()
        self.assertTrue(after["stale"])
        self.assertEqual(after["agents"], good["agents"])
        self.assertEqual(after["removed"], [])
        self.assertEqual(scan_agents.problems(after), [])

    def test_unusable_heals_a_dirty_previous_document(self):
        self.put(descriptor("alpha-agent", 47401))
        prev = document(self.scan(launchd={}), mcp_scan(), None)
        prev["agents"][0]["summary"] = "see " + keyed("SECRETPV", "h.example.com")
        prev["degraded"] = "not a list"
        out = scan_agents.unusable(prev, "TypeError", TODAY)
        self.assertNotIn("SECRETPV", json.dumps(out))
        self.assertEqual(len(out["degraded"]), 1)
        self.assertEqual(scan_agents.problems(out), [])

    def test_unusable_sanitises_the_reason_it_writes(self):
        out = scan_agents.unusable({"agents": [], "scanned_on": "2026-10-01"},
                                   "see " + keyed("SECRETWY", "h"), TODAY)
        self.assertNotIn("SECRETWY", json.dumps(out))

    def test_a_name_only_the_whole_stem_rule_flags_is_not_read(self):
        """`a: ?k=SECRET` holds a `scheme:` and a later `?` across a space: no single token is URL-shaped,
        so `_fix` leaves it, but the whole-stem `_suspect` refuses it."""
        name = "a: ?k=SECRETWS.json"
        (self.services / name).write_text(json.dumps(descriptor("alpha-agent", 47401)), encoding="utf-8")
        out = document(self.scan(launchd={}, platform="linux"), mcp_scan(), None)
        self.assertEqual(out["agents"], [])
        self.assertNotIn("SECRETWS", json.dumps(out))

    def test_a_name_the_sanitiser_would_change_is_not_read(self):
        """`_fix` changes an NFD name (it normalises to NFC) while `_suspect` does not flag it; such a
        name is not read today (the message wording is a filed row)."""
        nfd = "cafe\u0301-agent.json"
        (self.services / nfd).write_text(json.dumps(descriptor("alpha-agent", 47401)), encoding="utf-8")
        if not any(p.name == nfd for p in self.services.iterdir()):
            self.skipTest("this file system normalises names")
        out = document(self.scan(launchd={}, platform="linux"), mcp_scan(), None)
        self.assertEqual(out["agents"], [])


class FixCycle6(Fixture):
    """Review round 8 of #288: one invariant at the exit, and the PoCs that broke the shape checks."""

    def good_scan(self):
        self.put(descriptor("alpha-agent", 47401))
        self.put(descriptor("beta-agent", 47402))
        return self.scan(launchd={"com.example.alpha-agent": {"pid": 4242, "status": 0}})

    # --- MEDIUM 1: rows without ids are a partial scan, never a fresh empty estate ------------
    def test_rows_without_ids_make_the_scan_unusable_and_keep_first_seen(self):
        previous = document(self.good_scan(), mcp_scan(), None)
        for junk in ([{}, {"name": "x"}], ["row"], [{"id": 5}]):
            scan = self.good_scan()
            scan["services"] = junk
            with self.subTest(rows=junk), self.assertRaises(ValueError):
                document(scan, mcp_scan(), previous)
        kept = scan_agents.unusable(previous, "ValueError", TODAY)
        later = document(self.good_scan(), mcp_scan(), json.loads(json.dumps(kept)))
        self.assertEqual([r["first_seen_on"] for r in later["agents"]],
                         [r["first_seen_on"] for r in previous["agents"]], "first-seen survives (A1b)")

    # --- MEDIUM 2: the exit invariant -----------------------------------------------------------
    def test_document_never_returns_what_problems_refuses(self):
        cases = {
            "supervisor-bogus": lambda r: r.__setitem__("supervisor", {"state": "bogus"}),
            "supervisor-list": lambda r: r.__setitem__("supervisor", ["running"]),
            "manifest-string": lambda r: r.__setitem__("manifest", "x"),
            "port-string": lambda r: r.__setitem__("port", "47401"),
            "valid-string": lambda r: r.__setitem__("valid", "yes"),
            "problems-int": lambda r: r.__setitem__("problems", 5),
            "supervisor-extra": lambda r: r["supervisor"].__setitem__("tokenFile", "~/x"),
        }
        for name, mutate in cases.items():
            scan = self.good_scan()
            mutate(scan["services"][0])
            with self.subTest(case=name), self.assertRaises(ValueError):
                document(scan, mcp_scan(), None)
        scan = self.good_scan()
        scan["services"].append(dict(scan["services"][0]))
        with self.subTest(case="dup-ids"), self.assertRaises(ValueError):
            document(scan, mcp_scan(), None)

    def test_an_auth_block_in_a_scan_row_is_never_written(self):
        scan = self.good_scan()
        scan["services"][0]["auth"] = {"tokenFile": "~/fixture/SECRETTOKENPATH"}
        scan["services"][0]["undeclared"] = "SECRETUNDECLARED"
        out = document(scan, mcp_scan(), None)
        text = json.dumps(out)
        self.assertNotIn("SECRETTOKENPATH", text)
        self.assertNotIn("SECRETUNDECLARED", text)
        self.assertNotIn('"auth"', text)
        self.assertEqual(scan_agents.problems(out), [])

    def test_problems_checks_every_row_field_type(self):
        out = document(self.good_scan(), mcp_scan(), None)
        for field, value in (("valid", "yes"), ("commands", ["rm"]), ("first_seen_on", "today"),
                             ("mcp_declarations", "x"), ("supervisor", "running"), ("extra", 1)):
            bad = json.loads(json.dumps(out))
            bad["agents"][0][field] = value
            self.assertTrue(scan_agents.problems(bad), field)

    # --- MEDIUM 3: any punctuation is a home boundary ---------------------------------------------
    def test_home_after_any_punctuation_is_written_with_a_tilde(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            for sep in ("`", "[", ",", ";", "<", "{", "|", "*", "!", "+"):
                with self.subTest(sep=sep):
                    out = scan_agents._text(f"logs{sep}/srv/alpha/Library/Logs")
                    self.assertEqual(out, f"logs{sep}~/Library/Logs")
                    self.assertEqual(scan_agents.problems(tiny_doc(out)), [])
                    self.assertTrue(scan_agents.problems(tiny_doc(f"logs{sep}/srv/alpha/x")))
            self.assertEqual(scan_agents._text("x/srv/alpha/y"), "x/srv/alpha/y", "a path character is no boundary")
            self.assertEqual(scan_agents._text("~/srv/alpha/y"), "~/srv/alpha/y", "idempotent after ~")

    def test_home_after_punctuation_reaches_neither_registry_nor_wiki(self):
        """e2e_home.py of round 8, through the real emit and the wiki projection."""
        import workspace
        from emitter_fixture import environment, seed
        home = str(self.home)
        for i, summary in enumerate([f"Logs in `{home}/Library/Logs/alpha`", f"state [{home}/.alpha]",
                                     f"cfg,{home}/x", f"see {home}/Library/Logs"]):
            doc = descriptor(f"a{i}-agent", 47410 + i, manager="none")
            doc["summary"] = summary
            self.put(doc)
        root = self.base / "estate"
        root.mkdir()
        seed(root)
        workspace.initialize(root / "home", identities=False)
        settings = root / "home" / "config" / "settings.json"
        conf = json.loads(settings.read_text(encoding="utf-8"))
        conf.setdefault("features", {})["wiki_projection"] = True
        settings.write_text(json.dumps(conf), encoding="utf-8")
        raw, reg, vault = root / "raw", root / "registry", root / "vault"
        (raw / "agents.json").write_text(json.dumps(self.scan(launchd={}, platform="linux",
                                                              now=datetime.now(timezone.utc))), encoding="utf-8")
        env = {**environment(root), "OBSERVATORY_HOME": str(root / "home"), "OBSERVATORY_VAULT": str(vault)}
        (vault / "inventory").mkdir(parents=True)
        for script, args in (("collectors/emit_registry.py", [str(raw)]), ("tools/project_into_vault.py", [])):
            p = subprocess.run([sys.executable, script, *args], cwd=ROOT, env=env, capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr[-400:])
        for f in (reg / "agents.json", vault / "inventory" / "agents.json"):
            self.assertEqual(f.read_text(encoding="utf-8").count(home), 0, f)
        self.assertEqual(len(json.loads((reg / "agents.json").read_text(encoding="utf-8"))["agents"]), 4)

    # --- LOW: the surviving mutants ---------------------------------------------------------------
    def test_valid_sources_without_services_are_refused(self):
        scan = self.good_scan()
        del scan["services"]
        with self.assertRaises(TypeError):
            document(scan, mcp_scan(), None)

    def test_unusable_handles_hand_edited_previous_documents(self):
        previous = document(self.good_scan(), mcp_scan(), None)
        bad = json.loads(json.dumps(previous))
        bad["degraded"] = 5
        out = scan_agents.unusable(bad, "TypeError", TODAY)
        self.assertEqual(len(out["degraded"]), 1)
        self.assertIsNone(scan_agents.unusable({"degraded": []}, "TypeError", TODAY), "no agents list")
        bad = json.loads(json.dumps(previous))
        bad["scanned_on"] = 5
        out = scan_agents.unusable(bad, "TypeError", TODAY)
        self.assertIn("rows are from an unknown day", out["degraded"][-1]["reason"])


class FixCycle7(Fixture):
    """Review round 9 of #288: home paths with doubled slashes, dot segments or the firmlink."""

    FORMS = ("//srv/alpha", "/./srv/alpha", "/../srv/alpha", "///./../srv/alpha")

    def test_the_four_forms_are_written_with_a_tilde(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            for form in self.FORMS:
                with self.subTest(form=form):
                    out = scan_agents._text(f"state in {form}/.alpha")
                    self.assertEqual(out, "state in ~/.alpha")
                    self.assertEqual(scan_agents.problems(tiny_doc(out)), [])
            with patch.object(scan_agents.sys, "platform", "darwin"):
                self.assertEqual(scan_agents._text("at /System/Volumes/Data/srv/alpha/x"), "at ~/x")
                self.assertEqual(scan_agents._text("at /system/volumes/data//SRV/alpha/x"), "at ~/x")
            with patch.object(scan_agents.sys, "platform", "linux"):
                self.assertNotEqual(scan_agents._text("at /System/Volumes/Data/srv/alpha/x"), "at ~/x")

    def test_underscore_and_hyphen_are_boundaries(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            self.assertEqual(scan_agents._text("x_/srv/alpha/y and x-/srv/alpha"), "x_~/y and x-~")

    def test_the_four_forms_reach_neither_registry_nor_wiki(self):
        """e2e_r9.py: the four forms in a summary and a manifest path, through emit and projection."""
        import workspace
        from emitter_fixture import environment, seed
        home = str(self.home)
        man = self.home / "fixture" / "m" / "fabric-agent.json"
        man.parent.mkdir(parents=True)
        man.write_text(json.dumps(manifest_doc("alpha-agent")), encoding="utf-8")
        forms = ["/" + home, "/." + home, "/.." + home]
        if sys.platform == "darwin":
            forms.append("/System/Volumes/Data" + home)          # the firmlink exists only on macOS
        for i, h in enumerate(forms):
            doc = descriptor(f"a{i}-agent", 47420 + i, manager="none")
            doc["summary"] = f"state in {h}/.alpha"
            doc["fabricManifest"] = h + "/fixture/m/fabric-agent.json"
            self.put(doc)
        root = self.base / "estate"
        root.mkdir()
        seed(root)
        workspace.initialize(root / "home", identities=False)
        settings = root / "home" / "config" / "settings.json"
        conf = json.loads(settings.read_text(encoding="utf-8"))
        conf.setdefault("features", {})["wiki_projection"] = True
        settings.write_text(json.dumps(conf), encoding="utf-8")
        raw, reg, vault = root / "raw", root / "registry", root / "vault"
        (raw / "agents.json").write_text(json.dumps(self.scan(launchd={}, platform="linux",
                                                              now=datetime.now(timezone.utc))), encoding="utf-8")
        env = {**environment(root), "OBSERVATORY_HOME": str(root / "home"), "OBSERVATORY_VAULT": str(vault)}
        (vault / "inventory").mkdir(parents=True)
        for script, args in (("collectors/emit_registry.py", [str(raw)]), ("tools/project_into_vault.py", [])):
            p = subprocess.run([sys.executable, script, *args], cwd=ROOT, env=env, capture_output=True,
                               text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr[-400:])
        for f in (reg / "agents.json", vault / "inventory" / "agents.json"):
            text = f.read_text(encoding="utf-8")
            self.assertEqual(text.count(home), 0, f)
        doc = json.loads((reg / "agents.json").read_text(encoding="utf-8"))
        self.assertEqual(len(doc["agents"]), len(forms))
        self.assertTrue(all(r["manifest"]["state"] == "pinned" for r in doc["agents"]))

    def test_a_home_form_the_sanitiser_leaves_makes_the_document_unusable(self):
        doc = descriptor("alpha-agent", 47401)
        doc["summary"] = f"backup at x{self.home}/data"             # a letter before it: left alone
        self.put(doc)
        self.assertNotEqual(scan_agents._text(doc["summary"]).count(str(self.home)), 0, "the sanitiser leaves it")
        with self.assertRaises(ValueError):
            document(self.scan(launchd={}), mcp_scan(), None)

    def test_problems_checks_every_string_independently(self):
        self.put(descriptor("alpha-agent", 47401))
        out = document(self.scan(launchd={}), mcp_scan(), None)
        upper = str(self.home).upper() + "/x"
        for planted in (str(self.home), "//" + str(self.home)[1:] + "/x", upper):
            bad = json.loads(json.dumps(out))
            bad["agents"][0]["summary"] = planted
            found = scan_agents.problems(bad)
            if planted == upper and sys.platform not in ("darwin", "win32"):
                continue  # a case-sensitive filesystem: the upper-cased form names another path
            self.assertTrue(any("home path" in p for p in found), planted)

    def test_a_boolean_port_is_refused(self):
        self.put(descriptor("alpha-agent", 47401))
        out = document(self.scan(launchd={}), mcp_scan(), None)
        out["agents"][0]["port"] = True
        self.assertTrue(any("port" in p for p in scan_agents.problems(out)))


class FixCycle8(Fixture):
    """Review round 8 (adversarial) of #288: a home written in an equivalent lexical form.

    `home_leaks` used to read home as its parts joined by exactly one separator, so a path with a
    doubled separator, a `.` segment, an `x/..` detour, a backslash, a JSON escape, percent-encoding
    or fullwidth characters passed `problems()` and reached registry and wiki. Each form is
    generated here for a home, planted in every string field, and must be flagged."""

    @staticmethod
    def forms(home: str, user_tail: str = "DATA/x") -> dict[str, str]:
        """Equivalent spellings of `home + '/' + user_tail`, keyed by what makes each one differ."""
        parts = home.strip("/").split("/")
        last = parts[-1]
        head = "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""
        plain = home + "/" + user_tail
        out = {
            "double-separator": head + "//" + last + "/" + user_tail,
            "separator-run": "///" + "///".join(parts) + "/" + user_tail,
            "dot-segment": head + "/./" + last + "/" + user_tail,
            "dot-segments-everywhere": "/./" + "/./".join(parts) + "/./" + user_tail,
            "dot-dot-detour": head + "/zz/../" + last + "/" + user_tail,
            "dot-dot-deep": head + "/a/b/../../" + last + "/" + user_tail,
            "backslash-before-name": head + "/\\" + last + "/" + user_tail,
            "backslashes": plain.replace("/", "\\"),
            "doubled-backslashes": plain.replace("/", "\\\\"),
            "json-escaped-slashes": plain.replace("/", "\\/"),
            "json-unicode-escape": plain.replace("/", "\\u002f"),
            "percent-encoded": plain.replace("/", "%2F"),
            "percent-encoded-lower": plain.replace("/", "%2f"),
            "percent-encoded-twice": plain.replace("/", "%252F"),
            "fullwidth-separators": plain.replace("/", "\uff0f"),
            "fullwidth-name": head + "/" + "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c
                                                   for c in last) + "/" + user_tail,
            "tilde-user": "~" + last + "/" + user_tail,
            "fullwidth-tilde-user": "\uff5e" + last + "/" + user_tail,
            "tilde-user-alone": "~" + last,
        }
        out["json-string-of-backslashes"] = json.dumps({"p": plain.replace("/", "\\")})
        out["json-string-escaped"] = json.dumps({"p": plain}).replace("/", "\\/")
        return out

    BENIGN = ("https://example.com/a/../b", "https://example.com/a%2Fb/c?x=1", "1.2.3", "v2026.10.19",
              "/usr/local/bin", "~/x/../y", "~", "50% off", "%2Fusr%2Flocal", "C:\\Program Files\\x",
              "a/./b//c", "caf\\u00e9", "\\u002fusr", "/other/../srv/alphabet", "~alphabet/x",
              "/srv/alpha-old/x", "/srv/alpha.bak", "/srv/alphabet")

    def test_every_equivalent_form_is_flagged_and_benign_strings_are_not(self):
        for platform in ("linux", "darwin"):
            with patch.object(scan_agents, "_home", return_value="/srv/alpha"), \
                    patch.object(scan_agents.sys, "platform", platform):
                for name, form in self.forms("/srv/alpha").items():
                    with self.subTest(platform=platform, form=name):
                        self.assertTrue(scan_agents.home_leaks("state in " + form + " now"), form)
                        self.assertTrue(any("home path" in p for p in scan_agents.problems(tiny_doc(form))))
                for text in self.BENIGN:
                    with self.subTest(platform=platform, benign=text):
                        self.assertFalse(scan_agents.home_leaks(text), text)

    def test_a_home_left_by_dot_dot_is_still_flagged(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            self.assertTrue(scan_agents.home_leaks("/srv/alpha/../other"))
            self.assertTrue(scan_agents.home_leaks("/srv/beta/../alpha/x"))
            # `_text` cuts with `…`; NFKC would read it as `...`, three name characters.
            self.assertTrue(scan_agents.home_leaks("/srv/zz/../alpha\u2026"))
            self.assertFalse(scan_agents.home_leaks("/srv/alpha..."))      # another name

    def test_the_normalisation_is_linear_on_hostile_input(self):
        with patch.object(scan_agents, "_home", return_value="/srv/alpha"):
            for text in ("a/.." * 20000, "%25" * 20000, "\\" * 40000, "./" * 20000):
                self.assertFalse(scan_agents.home_leaks(text))

    def test_every_string_field_of_a_document_is_checked(self):
        self.put(descriptor("alpha-agent", 47401, manifest=self.put_manifest("alpha-agent")))
        out = document(self.scan(launchd={"com.example.alpha-agent": {"pid": 1, "status": 0}}), mcp_scan(), None)
        self.assertEqual(scan_agents.problems(out), [])

        def paths(node, at=()):
            if isinstance(node, str):
                yield at
            elif isinstance(node, dict):
                for k, v in node.items():
                    yield from paths(v, at + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    yield from paths(v, at + (i,))
        fields = list(paths(out["agents"][0]))
        self.assertGreater(len(fields), 5)
        for name, form in self.forms(str(self.home)).items():
            for field in fields:
                with self.subTest(form=name, field=field):
                    bad = json.loads(json.dumps(out))
                    node = bad["agents"][0]
                    for k in field[:-1]:
                        node = node[k]
                    node[field[-1]] = "at " + form
                    self.assertTrue(any("home path" in p for p in scan_agents.problems(bad)))

    def test_a_descriptor_carrying_any_form_is_refused_whole(self):
        """The fields the round named: the scan never yields a fresh document holding a form."""
        for name, form in self.forms(str(self.home)).items():
            for field in ("summary", "name", "fabricManifest", "lifecycle.plist", "lifecycle.label",
                          "lifecycle.unit", "lifecycle.manager"):
                with self.subTest(form=name, field=field):
                    for f in self.services.iterdir():
                        f.unlink()
                    doc = descriptor("alpha-agent", 47401)
                    if field.startswith("lifecycle."):
                        doc["lifecycle"][field.split(".", 1)[1]] = form
                    else:
                        doc[field] = form
                    self.put(doc)
                    try:
                        out = document(self.scan(launchd={}), mcp_scan(), None)
                    except ValueError:
                        continue                                   # refused: the emit keeps it stale
                    # Not refused: the form must be gone, by the test's own reading, not the module's.
                    text = json.dumps(out, ensure_ascii=False)
                    self.assertNotIn(form, text)
                    self.assertNotIn(self.home.name, text)

    def test_the_forms_reach_neither_registry_nor_wiki(self):
        """Through emit and projection: a clean tick, then one whose scan carries the forms. The
        second is refused, the first document is kept stale, and no form of home is in either file."""
        import workspace
        from emitter_fixture import environment, seed
        home = str(self.home)
        forms = self.forms(home, ".alpha")
        root = self.base / "estate"
        root.mkdir()
        seed(root)
        workspace.initialize(root / "home", identities=False)
        settings = root / "home" / "config" / "settings.json"
        conf = json.loads(settings.read_text(encoding="utf-8"))
        conf.setdefault("features", {})["wiki_projection"] = True
        settings.write_text(json.dumps(conf), encoding="utf-8")
        raw, reg, vault = root / "raw", root / "registry", root / "vault"
        env = {**environment(root), "OBSERVATORY_HOME": str(root / "home"), "OBSERVATORY_VAULT": str(vault)}
        (vault / "inventory").mkdir(parents=True)

        def tick():
            (raw / "agents.json").write_text(json.dumps(self.scan(launchd={}, platform="linux",
                                                                  now=datetime.now(timezone.utc))),
                                             encoding="utf-8")
            for script, args in (("collectors/emit_registry.py", [str(raw)]),
                                 ("tools/project_into_vault.py", [])):
                p = subprocess.run([sys.executable, script, *args], cwd=ROOT, env=env, capture_output=True,
                                   text=True, timeout=300)
                self.assertEqual(p.returncode, 0, p.stderr[-400:])
            return p

        self.put(descriptor("clean-agent", 47419, manager="none"))
        tick()
        first = json.loads((reg / "agents.json").read_text(encoding="utf-8"))
        self.assertFalse(first["stale"])
        for i, key in enumerate(("double-separator", "dot-dot-detour", "backslash-before-name",
                                 "json-escaped-slashes", "percent-encoded", "fullwidth-separators",
                                 "tilde-user")):
            doc = descriptor(f"a{i}-agent", 47420 + i, manager="none")
            doc["summary"] = "state in " + forms[key]
            self.put(doc)
        tick()
        with patch.object(scan_agents, "_home", return_value=home):
            for f in (reg / "agents.json", vault / "inventory" / "agents.json"):
                text = f.read_text(encoding="utf-8")
                for key, form in forms.items():
                    self.assertNotIn(form, text, (f, key))
                data = json.loads(text)
                self.assertTrue(data["stale"], f)
                self.assertEqual([r["id"] for r in data["agents"]], ["agent:clean-agent.default"], f)
                self.assertFalse(any(scan_agents.home_leaks(s) for s in scan_agents._strings(data)), f)


class EmitHistory(Fixture):
    """The emit reads the previous registry document, so dates and removals persist across ticks."""

    def test_first_seen_and_removed_come_from_the_previous_registry(self):
        import workspace
        from emitter_fixture import environment, seed
        root = self.base / "estate"
        root.mkdir()
        seed(root)
        workspace.initialize(root / "home", identities=False)
        raw, reg = root / "raw", root / "registry"
        env = {**environment(root), "OBSERVATORY_HOME": str(root / "home")}

        def emit(scan):
            (raw / "agents.json").write_text(json.dumps(scan), encoding="utf-8")
            p = subprocess.run([sys.executable, "collectors/emit_registry.py", str(raw)], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(p.returncode, 0, p.stderr[-600:])
            return json.loads((reg / "agents.json").read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc)
        self.put(descriptor("alpha-agent", 47401))
        self.put(descriptor("beta-agent", 47402))
        first = emit(self.scan(launchd={}, now=now))
        self.assertEqual(first["removed"], [])
        first["agents"][0]["first_seen_on"] = "2026-01-01"           # as an earlier tick recorded it
        (reg / "agents.json").write_text(json.dumps(first), encoding="utf-8")
        (self.services / "beta-agent.default.json").unlink()
        later = emit(self.scan(launchd={}, now=now))
        self.assertEqual(later["agents"][0]["first_seen_on"], "2026-01-01")
        self.assertEqual(later["removed"], ["agent:beta-agent.default"])
        good = json.loads((reg / "agents.json").read_text(encoding="utf-8"))
        self.assertFalse(good["stale"])
        for broken in (json.dumps(["not", "a", "scan"]), None):           # a list scan, then no scan at all
            with self.subTest(scan="list" if broken else "missing"):
                if broken is None:
                    (raw / "agents.json").unlink()
                else:
                    (raw / "agents.json").write_text(broken, encoding="utf-8")
                p = subprocess.run([sys.executable, "collectors/emit_registry.py", str(raw)], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=300)
                self.assertEqual(p.returncode, 0, "a broken scan never stops the emit, so never the tick")
                after = json.loads((reg / "agents.json").read_text(encoding="utf-8"))
                self.assertTrue(after["stale"], "a scan that could not be used is never hidden")
                self.assertEqual(after["agents"], good["agents"], "the last rows are kept")
                lines = [d["reason"] for d in after["degraded"] if d["reason"].startswith(scan_agents.UNUSABLE)]
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(("TypeError" if broken else "FileNotFoundError"), lines[0])
                self.assertIn(f"rows are from {good['scanned_on']}", lines[0])
                self.assertEqual(scan_agents.problems(after), [])


if __name__ == "__main__":
    unittest.main()
