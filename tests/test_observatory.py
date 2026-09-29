from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from observatory import core, credentials, dashboard

ROOT = Path(__file__).resolve().parents[1]


class ObservatoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.environment = patch.dict(os.environ, {"HOME": str(self.base / "empty-home"),
            "XDG_CONFIG_HOME": str(self.base / "empty-xdg"), "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1"}, clear=True)
        self.environment.start()
        self.state = self.base / "state"
        self.project = self.base / "project"
        self.project.mkdir()
        core.init(self.state)

    def tearDown(self):
        self.environment.stop()
        self.tmp.cleanup()

    def cli(self, *args, stdin=None, expected=0):
        env = {**os.environ, "HOME": str(self.base / "empty-home"), "PYTHONPATH": str(ROOT)}
        result = subprocess.run([sys.executable, "-m", "observatory", "--home", str(self.state), *args],
                                input=stdin, text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_init_sterile_idempotent(self):
        before = core.config(self.state)
        self.assertEqual(before, core.init(self.state))
        self.assertFalse((self.state / "latest.json").exists())
        self.assertEqual(before["projects"], [])

    def test_module_import_has_no_io(self):
        home = self.base / "import-home"
        home.mkdir()
        result = subprocess.run([sys.executable, "-c", "import observatory.core, observatory.cli"],
                                env={**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT)}, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(list(home.iterdir()), [])

    def test_discovery_does_not_register_or_follow_symlinks(self):
        (self.project / "package.json").write_text("{}")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "pyproject.toml").write_text("")
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        result = core.discover(self.project)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(core.config(self.state)["projects"], [])

    def test_name_and_scope_validation(self):
        for name in ("../escape", "name/secret", "x\nunsafe", "1name"):
            with self.assertRaises(core.ObservatoryError):
                core.add_project(self.state, name, str(self.project))
        with self.assertRaises(core.ObservatoryError):
            core.add_project(self.state, "bad", str(self.base))
        core.add_project(self.state, "example", str(self.project))
        with self.assertRaises(core.ObservatoryError):
            core.add_project(self.state, "other", str(self.project))

    def test_missing_root_is_partial_not_empty_success(self):
        core.add_project(self.state, "example", str(self.project))
        self.project.rmdir()
        report = core.scan(self.state)
        self.assertTrue(report["projects"][0]["degraded"])
        self.assertEqual(report["findings"][0]["kind"], "observation.partial")

    def test_env_only_metadata_salted_per_installation(self):
        sentinel = "synthetic-value-with-no-provider-identity"
        (self.project / ".env").write_text(f"EXAMPLE_TOKEN={sentinel}\nEMPTY=\n")
        (self.project / ".env").chmod(0o644)
        core.add_project(self.state, "example", str(self.project))
        report = core.scan(self.state)
        serialized = json.dumps(report)
        self.assertNotIn(sentinel, serialized)
        self.assertNotIn(str(self.project), serialized)
        fingerprint = report["projects"][0]["environment"][0]["variables"][0]["fingerprint"]
        second = self.base / "second-state"
        core.init(second)
        core.add_project(second, "example", str(self.project))
        other = core.scan(second)["projects"][0]["environment"][0]["variables"][0]["fingerprint"]
        self.assertNotEqual(fingerprint, other)
        self.assertIn("env.permissions", {f["kind"] for f in report["findings"]})
        export = json.dumps(core.public_export(report))
        for sensitive in (sentinel, fingerprint, "EXAMPLE_TOKEN", "example"):
            self.assertNotIn(sensitive, export)

    def test_env_parser_never_expands_or_executes(self):
        marker = self.base / "should-not-exist"
        (self.project / ".env").write_text(f"X=$(touch {marker})\nY='literal'\n")
        result = core.env_pairs(self.project / ".env")
        self.assertEqual(result[1], ("Y", "literal"))
        self.assertFalse(marker.exists())

    def test_symlink_env_is_ignored(self):
        external = self.base / "private-file"
        external.write_text("HIDDEN=value")
        (self.project / ".env").symlink_to(external)
        core.add_project(self.state, "example", str(self.project))
        self.assertEqual(core.scan(self.state)["projects"][0]["environment"], [])

    def test_history_retention_and_permissions(self):
        core.add_project(self.state, "example", str(self.project))
        for _ in range(103):
            core.scan(self.state)
        with contextlib.closing(sqlite3.connect(self.state / "history.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 100)
        if os.name == "posix":
            self.assertEqual((self.state / "latest.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.state / "history.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_secret_stdin_and_child_output_suppressed(self):
        sentinel = "synthetic-stdin-value-never-printed"
        result = self.cli("secret", "put", "EXAMPLE", stdin=sentinel + "\n")
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual(self.cli("secret", "list")["names"], ["EXAMPLE"])
        result = self.cli("secret", "run", "--env", "EXAMPLE_TOKEN", "EXAMPLE", "--", sys.executable,
                          "-c", "import os; print(os.environ['EXAMPLE_TOKEN'])")
        self.assertEqual(result["exit_code"], 0)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertNotIn(sentinel, json.dumps(self.cli("export")))

    def test_private_secret_permissions_and_symlink_refused(self):
        credentials.put_secret(self.state, "EXAMPLE", "synthetic-example-value")
        file = self.state / "secrets" / "EXAMPLE"
        if os.name == "posix":
            file.chmod(0o644)
            with self.assertRaises(core.ObservatoryError):
                credentials.read_secret(self.state, "EXAMPLE")
        file.unlink()
        file.symlink_to(self.project / "outside")
        with self.assertRaises(core.ObservatoryError):
            credentials.put_secret(self.state, "EXAMPLE", "new-synthetic-value")

    def test_known_value_file_scan_no_content_or_paths(self):
        sentinel = "synthetic-matching-value"
        credentials.put_secret(self.state, "EXAMPLE", sentinel)
        target = self.base / "synthetic-log"
        target.write_text(sentinel + "\n" + sentinel)
        report = credentials.scan_leaks(self.state, [str(target)])
        self.assertEqual(report["targets"][0]["occurrences"], 2)
        self.assertNotIn(sentinel, json.dumps(report))
        self.assertNotIn(str(target), json.dumps(report))
        self.cli("leaks", "scan", "--file", str(target), expected=1)
        target.write_text("unrelated unknown material")
        self.cli("leaks", "scan", "--file", str(target))

    def test_scan_no_known_values_is_degraded(self):
        target = self.base / "empty-log"
        target.write_text("")
        result = credentials.scan_leaks(self.state, [str(target)])
        self.assertTrue(result["degraded"])

    def test_scan_refuses_secret_store_as_target(self):
        credentials.put_secret(self.state, "EXAMPLE", "synthetic-example-value")
        result = credentials.scan_leaks(self.state, [str(self.state / "secrets" / "EXAMPLE")])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["findings"], [])

    def test_sqlite_scan_is_readonly_and_quotes_identifiers(self):
        sentinel = "synthetic-sqlite-value"
        credentials.put_secret(self.state, "EXAMPLE", sentinel)
        target = self.base / "synthetic-memory?.sqlite3"
        with contextlib.closing(sqlite3.connect(target)) as db, db:
            db.execute('CREATE TABLE "odd""name" (body TEXT)')
            db.execute('INSERT INTO "odd""name" VALUES (?)', (sentinel,))
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        result = credentials.scan_leaks(self.state, [str(target)], sqlite=True)
        self.assertEqual(result["targets"][0]["occurrences"], 1)
        self.assertEqual(before, hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertNotIn(sentinel, json.dumps(result))

    def test_git_real_workflow_reports_ahead_without_network(self):
        remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        def git(*args):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(self.project), *args], check=True, capture_output=True)
        git("init", "-q")
        (self.project / "file.txt").write_text("one")
        git("add", ".")
        git("commit", "-qm", "one")
        git("remote", "add", "origin", str(remote))
        git("push", "-u", "origin", "HEAD")
        (self.project / "file.txt").write_text("two")
        git("commit", "-qam", "two")
        (self.project / "untracked.txt").write_text("pending")
        result = core.git_metrics(self.project)
        self.assertEqual(result["ahead"], 1)
        self.assertEqual(result["behind"], 0)
        self.assertEqual(result["dirty_files"], 1)

    def test_git_clean_filter_never_executes(self):
        def git(*args):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(self.project), *args], check=True, capture_output=True)
        git("init", "-q")
        marker = self.base / "filter-executed"
        (self.project / "tracked").write_text("one")
        (self.project / ".gitattributes").write_text("tracked filter=probe\n")
        git("add", ".")
        git("commit", "-qm", "fixture")
        git("config", "filter.probe.clean", f"touch '{marker}'; cat")
        (self.project / "tracked").write_text("two")
        os.utime(self.project / "tracked", (1, 1))
        report = core.git_metrics(self.project)
        self.assertTrue(report["readable"])
        self.assertFalse(marker.exists())

    def test_registered_root_replaced_by_symlink_is_refused(self):
        core.add_project(self.state, "example", str(self.project))
        self.project.rename(self.base / "old-project")
        external = self.base / "external"
        external.mkdir()
        (external / ".env").write_text("OUTSIDE=synthetic-outside-value")
        self.project.symlink_to(external, target_is_directory=True)
        report = core.scan(self.state)
        self.assertTrue(report["projects"][0]["degraded"])
        self.assertEqual(report["projects"][0]["environment"], [])

    def test_metadata_canary_redacted_in_names_paths_and_slot_labels(self):
        value = "SYNTHETIC_CANARY_12345678"
        result = credentials.put_secret(self.state, value, value)
        self.assertNotIn(value, json.dumps(result))
        (self.project / (".env." + value)).write_text(f"{value}={value}\n")
        core.add_project(self.state, value, str(self.project))
        report = core.scan(self.state)
        self.assertNotIn(value, json.dumps(report))
        target = self.base / "canary-log"
        target.write_text(value)
        leak = credentials.scan_leaks(self.state, [str(target)])
        self.assertNotIn(value, json.dumps(leak))
        self.assertNotIn(value, (self.state / "latest.json").read_text())
        self.assertNotIn(value, json.dumps(self.cli("secret", "list")))

    def test_export_refuses_untyped_snapshot_fields(self):
        for report in ({"schema_version": 1, "scanned_at": "SYNTHETIC_PRIVATE_VALUE", "projects": [], "findings": []},
                       {"schema_version": 1, "scanned_at": None, "projects": [{"files": "canary", "bytes": 0}], "findings": []}):
            with self.assertRaises(core.ObservatoryError):
                core.public_export(report)
        core.write_json(self.state / "latest.json", {"schema_version": 1, "scanned_at": "bad"})
        self.cli("export", expected=2)

    def test_sqlite_generated_payload_is_not_evaluated(self):
        credentials.put_secret(self.state, "EXAMPLE", "AAAAAAAA")
        target = self.base / "generated.sqlite3"
        with contextlib.closing(sqlite3.connect(target)) as db, db:
            db.execute("CREATE TABLE t(x INTEGER, payload TEXT GENERATED ALWAYS AS(replace(hex(zeroblob(9000000)), '0', 'A')) VIRTUAL)")
            db.execute("INSERT INTO t(x) VALUES (1)")
        report = credentials.scan_leaks(self.state, [str(target)], sqlite=True)
        self.assertTrue(report["degraded"])
        self.assertEqual(report["findings"], [])

    def test_dashboard_escapes_and_omits_credentials(self):
        report = {"scanned_at": "<script>alert(1)</script>", "projects": [], "findings": []}
        page = dashboard.render(report)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("observatory-token", page)

    def test_dashboard_escapes_numeric_fields_and_validator_refuses_them(self):
        report = {"schema_version": 1, "scanned_at": None, "projects": [
            {"name": "sample", "degraded": [], "files": 1, "bytes": 1,
             "git": {"dirty_files": "<img src=x>", "ahead": "<script>x</script>"}}], "findings": []}
        self.assertNotIn("<img", dashboard.render(report))
        self.assertNotIn("<script>", dashboard.render(report))
        with self.assertRaises(core.ObservatoryError):
            core.validate_report(report)

    def test_dashboard_binds_without_asking_the_resolver(self):
        # HTTPServer.server_bind() calls socket.getfqdn() before listen(); a slow resolver on a
        # Mac leaves the port bound but silent (seen on the macOS CI runner).
        with patch("socket.getfqdn", side_effect=AssertionError("resolver asked at bind")):
            server = dashboard.LoopbackServer(("127.0.0.1", 0), dashboard.handler(self.state))
        server.server_close()
        self.assertEqual(server.server_name, "127.0.0.1")

    def test_http_rejects_rebinding_origin_prefix_and_writes(self):
        server = dashboard.http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.handler(self.state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            def request(method="GET", **headers):
                connection = http.client.HTTPConnection("127.0.0.1", port)
                connection.request(method, "/", headers=headers)
                response = connection.getresponse()
                body = response.read()
                connection.close()
                return response, body
            response, _ = request()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
            self.assertEqual(request(Host=f"localhost.evil.invalid:{port}")[0].status, 403)
            self.assertEqual(request(Origin="http://localhost.evil.invalid")[0].status, 403)
            self.assertEqual(request("POST")[0].status, 405)
        finally:
            server.shutdown()
            server.server_close()

    def test_complete_cli_demo_is_synthetic(self):
        result = self.cli("demo")
        self.assertEqual(result["projects"], 1)
        self.assertEqual(result["known_value_occurrences"], 2)
        self.assertEqual(result["network_requests"], 0)
        self.assertTrue(Path(result["dashboard"]).is_file())
        self.assertEqual(self.cli("status")["projects"][0]["name"], "sample-app")
        self.assertEqual(self.cli("export")["project_count"], 1)
        self.cli("doctor")

    def test_public_release_gate_refuses_tokens_private_identifiers_and_state(self):
        spec = importlib.util.spec_from_file_location("privacy_gate", ROOT / "tools/check_public_release.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        candidate = self.base / "public-candidate"
        candidate.mkdir()
        doc = candidate / "README.md"
        doc.write_text("A generic public project.")
        self.assertTrue(gate.audit(candidate, [], False)["passed"])
        doc.write_text("gh" + "p_" + "A" * 40)
        self.assertFalse(gate.audit(candidate, [], False)["passed"])
        doc.write_text("synthetic-private-identifier")
        report = gate.audit(candidate, ["synthetic-private-identifier"], False)
        self.assertFalse(report["passed"])
        self.assertNotIn("synthetic-private-identifier", json.dumps(report))
        doc.write_text("generic")
        (candidate / "registry").mkdir()
        (candidate / "registry" / "inventory.json").write_text("{}")
        self.assertFalse(gate.audit(candidate, [], False)["passed"])

    def test_public_release_gate_rejects_tracked_cache_and_deleted_history(self):
        spec = importlib.util.spec_from_file_location("privacy_gate", ROOT / "tools/check_public_release.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        candidate = self.base / "public-git-candidate"
        candidate.mkdir()
        def git(*args):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(candidate), *args], check=True, capture_output=True)
        git("init", "-q")
        (candidate / "README.md").write_text("identical generic content")
        (candidate / "build").mkdir()
        (candidate / "build" / "cached.txt").write_text("identical generic content")
        git("add", ".")
        self.assertFalse(gate.audit(candidate, [], False)["passed"])
        git("commit", "-qm", "fixture")
        git("rm", "build/cached.txt")
        git("commit", "-qm", "remove cached path")
        self.assertTrue(gate.audit(candidate, [], False)["passed"])
        self.assertFalse(gate.audit(candidate, [], True)["passed"])


if __name__ == "__main__":
    unittest.main()
