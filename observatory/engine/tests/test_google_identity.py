"""GA4 resource identity prevents multiple credentials multiplying traffic."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "collectors"), str(ROOT / "plugins")]
import google_registry as registry
import hostmap


def observation(source="one@example.invalid", **changes):
    return {"property": "properties/123", "name": "Synthetic product",
            "account": "accounts/9", "account_name": "Synthetic account",
            "hosts": [], "app_ids": [], "users_30d": 100, "sessions_30d": 150,
            "views_30d": 200, "read_with": source, **changes}


class GoogleIdentityTests(unittest.TestCase):
    def test_same_id_counts_once_and_retains_credential_provenance(self):
        original = [observation(), observation("two@example.invalid")]
        before = copy.deepcopy(original)
        rows = registry.canonical_properties(original)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["users_30d"], 100)
        self.assertEqual(rows[0]["read_with_all"], ["one@example.invalid", "two@example.invalid"])
        self.assertEqual(rows[0]["observation_count"], 2)
        self.assertEqual(original, before)
        self.assertEqual(registry.canonical_properties(rows), rows)

    def test_same_name_with_different_ids_remains_two_properties(self):
        rows = registry.canonical_properties([observation(), observation(property="properties/456")])
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["property"] for row in rows}, {"properties/123", "properties/456"})

    def test_success_outweighs_error_in_both_enumeration_orders(self):
        good = observation("successful@example.invalid")
        bad = observation("failed@example.invalid", error="permission denied", users_30d=None,
                          sessions_30d=None, views_30d=None)
        for inputs in ([bad, good], [good, bad]):
            with self.subTest(order=[p["read_with"] for p in inputs]):
                row = registry.canonical_properties(inputs)[0]
                self.assertEqual(row["users_30d"], 100)
                self.assertNotIn("error", row)
                self.assertEqual(len(row["read_with_all"]), 2)

    def test_conflicting_successes_are_unknown_and_explicitly_degraded(self):
        inputs = [observation(), observation("two@example.invalid", users_30d=110)]
        a = registry.normalize_document({"properties": inputs})
        b = registry.normalize_document({"properties": list(reversed(inputs))})
        self.assertEqual(a, b)
        self.assertEqual(a["properties"][0]["observation_conflicts"], ["users_30d"])
        self.assertIn("conflicting", a["properties"][0]["error"])
        for field in ("users_30d", "sessions_30d", "views_30d"):
            self.assertIsNone(a["properties"][0][field])
        self.assertEqual(len(a["degraded"]), 1)
        self.assertIsNone(a["totals"]["users_30d"])
        self.assertEqual(a["totals"]["unknown_properties"], 1)
        self.assertEqual(registry.normalize_document(a), a)

    def test_conflicting_account_is_not_silently_selected(self):
        row = registry.canonical_properties([observation(), observation(account="accounts/10")])[0]
        self.assertIsNone(row["account"])
        self.assertIsNone(row["account_name"])
        self.assertIn("account", row["observation_conflicts"])

    def test_missing_id_is_not_deduplicated_by_display_name(self):
        rows = registry.canonical_properties([observation(property=None), observation(property=None)])
        self.assertEqual(len(rows), 2)

    def test_cached_registry_recomputes_property_account_and_project_totals(self):
        props = [{**observation(source), "id": "ga4:123", "project": "project:fixture",
                  "standing": "linked", "link_rule": "declared"}
                 for source in ("one@example.invalid", "two@example.invalid")]
        props.append({**observation(property="properties/456"), "id": "ga4:456", "project": None,
                      "standing": "unclaimed", "link_rule": None})
        doc = registry.normalize_document({"properties": props, "totals": {"properties": 3, "users_30d": 300},
            "accounts": [{"account": "accounts/9", "read_with": source} for source in
                         ("one@example.invalid", "two@example.invalid")]})
        self.assertEqual(doc["totals"]["properties"], 2)
        self.assertEqual(doc["totals"]["accounts"], 1)
        self.assertEqual(doc["totals"]["users_30d"], 200)
        self.assertEqual(doc["totals"]["users_30d_unclaimed"], 100)
        self.assertEqual(doc["totals"]["linked_to_a_project"], 1)
        self.assertEqual(doc["totals"]["by_rule"], {"declared": 1})
        self.assertEqual(sum(p["users_30d"] for p in doc["properties"] if p["project"] == "project:fixture"), 100)

    def test_project_totals_count_resources_and_report_unknown_coverage(self):
        good = observation(project="project:fixture", standing="linked", report_url="https://example.invalid/report")
        failed = observation("failed@example.invalid", property="properties/456", project="project:fixture",
                             error="permission denied", users_30d=None, sessions_30d=None, views_30d=None)
        rows = [good, {**good, "read_with": "two@example.invalid"}, failed]
        total = registry.normalize_document({"properties": rows})["totals"]
        project = registry.project_traffic(rows)["project:fixture"]
        for aggregate in (total, project):
            self.assertEqual(aggregate["users_30d"], 100)
            self.assertEqual(aggregate["measured_properties"], 1)
            self.assertEqual(aggregate["unknown_properties"], 1)
        self.assertEqual(len(project["properties"]), 2)
        self.assertEqual(project["properties"][0]["report_url"], "https://example.invalid/report")
        unknown = registry.project_traffic([failed])["project:fixture"]
        self.assertIsNone(unknown["users_30d"])
        self.assertEqual(unknown["unknown_properties"], 1)
        conflict = registry.project_traffic([good, {**good, "users_30d": 110}])["project:fixture"]
        self.assertIsNone(conflict["users_30d"])
        self.assertEqual(conflict["unknown_properties"], 1)

    def test_zero_is_measured_but_an_error_is_not(self):
        measured = observation(project="project:fixture", users_30d=0)
        failed = observation(property="properties/456", project="project:fixture", error="failed")
        for aggregate in (registry.normalize_document({"properties": [measured, failed]})["totals"],
                          registry.project_traffic([measured, failed])["project:fixture"]):
            self.assertEqual(aggregate["users_30d"], 0)
            self.assertEqual(aggregate["measured_properties"], 1)
            self.assertEqual(aggregate["unknown_properties"], 1)
        self.assertIsNone(registry.normalize_document({"properties": []})["totals"]["users_30d"])

    def test_findings_name_unknown_and_partial_traffic_without_inventing_zero(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import google_findings
        unknown = observation(name="Unknown fixture", standing="unclaimed", error="denied",
                              users_30d=None, sessions_30d=None, views_30d=None)
        finding = google_findings.findings({"properties": [unknown]})[0]
        self.assertIn("unknown traffic", finding["title"])
        self.assertIn("Unknown fixture (unknown)", finding["detail"])
        self.assertNotIn("0 user", finding["title"])
        known = observation(name="Measured fixture", property="properties/456", standing="unclaimed")
        partial = google_findings.findings({"properties": [known, unknown]})[0]
        self.assertIn("100 summed users", partial["title"])
        self.assertIn("1 unmeasured", partial["title"])
        self.assertIn("Unknown fixture (unknown)", partial["detail"])

    def test_emitter_accepts_unknown_totals_without_zero_or_formatting_failure(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import emitter_fixture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            env = emitter_fixture.seed(root)
            (root / "raw/google.json").write_text(json.dumps({"properties": [observation(
                error="permission denied", users_30d=None, sessions_30d=None, views_30d=None)]}))
            result = subprocess.run([sys.executable, str(ROOT / "collectors/emit_registry.py"), str(root / "raw")],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-1500:])
            self.assertIn("unknown summed users/30d", result.stdout)
            totals = json.loads((root / "registry/google-properties.json").read_text())["totals"]
            self.assertIsNone(totals["users_30d"])
            self.assertEqual(totals["unknown_properties"], 1)

    def test_raw_scan_emit_and_cached_normalizer_share_canonical_identity(self):
        scan = {"properties": [observation(), observation("two@example.invalid")],
                "accounts": [{"account": "accounts/9"}, {"account": "accounts/9"}]}
        urls = {key: "" for key in ("ga4_report", "ga4_admin", "search_console_site", "cloud_project")}
        with tempfile.TemporaryDirectory() as tmp, patch.object(hostmap, "build", return_value={}), \
                patch.object(registry, "boundary", return_value={}), patch.object(registry, "consoles", return_value=urls):
            rows = registry.rows(scan, [], Path(tmp) / "absent.json")
            doc = registry.document(scan, rows, "2026-01-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(doc["totals"]["users_30d"], 100)
        self.assertEqual(doc["totals"]["accounts"], 1)
        self.assertEqual(doc["properties"][0]["observation_count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
