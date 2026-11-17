#!/usr/bin/env python3
"""`machine_health.report()` answers `agent-health/1` (OBS-577, DEC-0179) and agrees with the contract's own files.

* the vendored files are the contract's bytes (digests in `fabric/agent-health/README.md`);
* every vector (`worst`, `read`, `episodes`) and fixture (positive, negative, semantic) runs through the engine's port;
* a report built from fixture inputs validates against the vendored schema and passes the report rules;
* a planted path or slot name never appears in the output; a source not read is `unknown`, never `ok`.

Values shaped like a credential are built from pieces here, never written whole.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import machine_health as MH  # noqa: E402

VENDORED = ROOT / "fabric" / "agent-health"
VECTORS = json.loads((VENDORED / "agent-health.json").read_text(encoding="utf-8"))


def schema_errors(value) -> list:
    import jsonschema
    from referencing import Registry, Resource
    docs = [json.loads((VENDORED / n).read_text(encoding="utf-8"))
            for n in ("agent-health.schema.json", "common.schema.json")]
    registry = Registry().with_resources([(d["$id"], Resource.from_contents(d)) for d in docs])
    return list(jsonschema.Draft202012Validator(docs[0], registry=registry).iter_errors(value))


def finding(kind: str, severity: str = "warning", **extra) -> dict:
    return {"type": kind, "subject": "alpha-web", "severity": severity, **extra}


def sources(findings=None, **extra) -> dict:
    return {"findings": [] if findings is None else findings, "integration": True, **extra}


def by_id(doc: dict) -> dict:
    return {c["id"]: c for c in doc["checks"]}


class TheVendoredFilesAreTheContractsBytes(unittest.TestCase):
    def test_every_digest_the_readme_names_matches(self) -> None:
        readme = (VENDORED / "README.md").read_text(encoding="utf-8")
        rows = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$", readme, re.M))
        files = sorted(p.relative_to(VENDORED).as_posix() for p in VENDORED.rglob("*.json"))
        self.assertEqual(sorted(rows), files)
        for name, digest in rows.items():
            self.assertEqual(hashlib.sha256((VENDORED / name).read_bytes()).hexdigest(), digest, name)

    def test_the_readme_names_the_commit(self) -> None:
        self.assertIn("db0d8f15994822724356d0a7e7ccc98dbcca604d", (VENDORED / "README.md").read_text(encoding="utf-8"))


class TheVectors(unittest.TestCase):
    def test_worst(self) -> None:
        for case in VECTORS["worst"]:
            self.assertEqual(MH.worst_state(case["input"]), case["expected"], case["name"])

    def test_read(self) -> None:
        for case in VECTORS["read"]:
            self.assertEqual(MH.read_health(case["input"]), case["expected"], case["name"])

    def test_episodes(self) -> None:
        for case in VECTORS["episodes"]:
            self.assertEqual(MH.episode_key(*case["input"]), case["expected"], case["name"])


class TheFixtures(unittest.TestCase):
    def test_positive_pass_schema_and_rules(self) -> None:
        for path in sorted((VENDORED / "positive").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema_errors(doc), [], path.name)
            self.assertEqual(MH.report_rules(doc), [], path.name)

    def test_negative_fail_schema(self) -> None:
        for path in sorted((VENDORED / "negative").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotEqual(schema_errors(doc), [], path.name)
            self.assertEqual(MH.read_health(doc)["state"], "unknown", path.name)

    def test_semantic_report_the_contracts_codes(self) -> None:
        for path in sorted((VENDORED / "semantic").glob("*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema_errors(doc["input"].get("health", doc["input"])), [], path.name)
            if doc["kind"] == "agent-health":
                found = MH.report_rules(doc["input"])
            else:
                found = MH.service_rules(doc["input"])
            self.assertEqual(sorted({f["code"] for f in found}), sorted(doc["expect"]), path.name)


class TheReport(unittest.TestCase):
    def build(self, **kw) -> dict:
        return MH.report(sources=kw.pop("sources", sources()), now=1794830000, version="1.2.3")

    def test_a_clean_machine_is_ok_and_valid(self) -> None:
        doc = self.build()
        self.assertEqual(schema_errors(doc), [])
        self.assertEqual(MH.report_rules(doc), [])
        self.assertEqual((doc["schema"], doc["product"], doc["version"], doc["at"], doc["state"]),
                         ("agent-health/1", "project-observatory", "1.2.3", 1794830000, "ok"))
        self.assertTrue(all(c["state"] == "ok" and c["count"] == 0 for c in doc["checks"]))

    def test_every_check_names_a_tool_or_none(self) -> None:
        for check in self.build()["checks"]:
            fix = check.get("fix")
            self.assertTrue(fix is None or set(fix) == {"tool"}, check["id"])
            self.assertTrue(check["area"] in ("environment", "update", "security", "connection"), check["id"])

    def test_disk_low_fail_at_critical_warn_at_low(self) -> None:
        crit = by_id(self.build(sources=sources([finding("machine.disk_low", "critical")])))["disk_low"]
        low = by_id(self.build(sources=sources([finding("machine.disk_low", "warning")])))["disk_low"]
        self.assertEqual((crit["state"], crit["count"]), ("fail", 1))
        self.assertEqual(low["state"], "warn")

    def test_memory_vm_and_state_is_the_worst(self) -> None:
        doc = self.build(sources=sources([finding("machine.memory_pressure"), finding("vm.disk_errors", "critical")]))
        checks = by_id(doc)
        self.assertEqual(checks["memory_pressure"]["state"], "warn")
        self.assertEqual(checks["vm_disk_errors"]["state"], "fail")
        self.assertEqual(doc["state"], "fail")
        self.assertEqual(MH.report_rules(doc), [])

    def test_security_checks_count_findings(self) -> None:
        doc = self.build(sources=sources([
            finding("secret.leaked_unrotated", "critical"), finding("secret.leaked_unrotated", "warning"),
            finding("credential.expiring"), finding("credential.expired", "critical"),
            finding("dep.vulnerable", exploited=True), finding("dep.vulnerable", exploited=False),
            finding("secret.in_repository", "critical"), finding("mcp.key_in_url"), finding("mcp.risky_command"),
            finding("ssh.key_weak"), finding("ssh.key_readable"), finding("harness.plugin_behind", "info")]))
        c = by_id(doc)
        self.assertEqual((c["keys_leaked_unrotated"]["state"], c["keys_leaked_unrotated"]["count"]), ("fail", 2))
        self.assertEqual((c["keys_expiring"]["state"], c["keys_expiring"]["count"]), ("fail", 2))
        self.assertEqual((c["dependencies_exploited"]["state"], c["dependencies_exploited"]["count"]), ("warn", 1))
        self.assertEqual((c["keys_in_repositories"]["state"], c["keys_in_repositories"]["count"]), ("fail", 1))
        self.assertEqual((c["mcp_risky"]["state"], c["mcp_risky"]["count"]), ("warn", 2))
        self.assertEqual((c["ssh_keys"]["state"], c["ssh_keys"]["count"]), ("warn", 2))
        self.assertEqual((c["plugins_behind"]["state"], c["plugins_behind"]["count"]), ("warn", 1))
        self.assertEqual(schema_errors(doc), [])

    def test_integration_keys_count_and_an_unread_report_is_unknown(self) -> None:
        doc = self.build(sources=sources([finding("integration.key_invalid"), finding("integration.drift")]))
        c = by_id(doc)["integration_keys_invalid"]
        self.assertEqual((c["area"], c["state"], c["count"], c["fix"]),
                         ("connection", "warn", 1, {"tool": "observatory_integrations"}))
        self.assertEqual(schema_errors(doc), [])
        unread = by_id(self.build(sources=sources([], integration=False)))["integration_keys_invalid"]
        self.assertEqual((unread["state"], unread["count"]), ("unknown", 0))

    def test_an_acknowledged_finding_does_not_count(self) -> None:
        doc = self.build(sources=sources([finding("ssh.key_weak", acked=True)]))
        self.assertEqual(by_id(doc)["ssh_keys"]["state"], "ok")

    def test_no_findings_file_is_unknown_never_ok(self) -> None:
        doc = self.build(sources={"findings": None})
        self.assertTrue(all(c["state"] == "unknown" for c in doc["checks"]))
        self.assertEqual(doc["state"], "unknown")
        self.assertEqual(schema_errors(doc), [])

    def test_a_scan_that_could_not_measure_is_unknown(self) -> None:
        doc = self.build(sources=sources([finding("host.disk_unknown"), finding("secret.leak_scan_unmeasured"),
                                          finding("dep.watch_stale"), finding("harness.scan_degraded")]))
        c = by_id(doc)
        for name in ("disk_low", "keys_leaked_unrotated", "keys_in_repositories", "dependencies_exploited",
                     "plugins_behind", "mcp_risky"):
            self.assertEqual(c[name]["state"], "unknown", name)
        self.assertEqual(c["ssh_keys"]["state"], "ok")

    def test_a_planted_path_or_slot_name_never_appears(self) -> None:
        root = "/" + "home" + "/someone"
        planted = {"detail": root + "/project/.env holds STRIPE_SECRET_KEY", "evidence": [root + "/x"]}
        doc = self.build(sources=sources([finding("secret.leaked_unrotated", "critical", subject="secret:STRIPE_SECRET_KEY",
                                                   **planted), finding("ssh.key_weak", subject=root + "/.ssh/id")]))
        text = json.dumps(doc)
        for needle in ("someone", "STRIPE", ".env", "home", ".ssh"):
            self.assertNotIn(needle, text)

    def test_default_sources_read_the_findings_file(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "findings.json").write_text(json.dumps({"findings": [finding("ssh.key_weak")]}), encoding="utf-8")
            read = MH.read_sources(Path(tmp), Path(tmp))
            self.assertEqual(len(read["findings"]), 1)
            self.assertFalse(read["integration"])
            Path(tmp, "integration-health.json").write_text(json.dumps({"checked_on": "2026-11-16"}), encoding="utf-8")
            self.assertTrue(MH.read_sources(Path(tmp), Path(tmp))["integration"])
            self.assertIsNone(MH.read_sources(Path(tmp, "missing"))["findings"])
            Path(tmp, "findings.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(MH.read_sources(Path(tmp))["findings"])


if __name__ == "__main__":
    unittest.main()
