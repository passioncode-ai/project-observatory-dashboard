#!/usr/bin/env python3
"""`full update`: a published release, verified twice, installed reversibly.

Everything runs against a fake GitHub release server on 127.0.0.1 and synthetic
wheels built here. The installer, the launchd jobs and the new engine's own
commands are injected fakes, so no test invokes pip, launchctl or the network,
and none of them touches the machine the suite runs on.
"""
from __future__ import annotations
import contextlib
import hashlib
import http.server
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import configuration as config
import workspace

REPO = "example-org/example-observatory"
CURRENT = config.VERSION


def bump(version: str, step: int) -> str:
    major, minor, patch_ = config.version_tuple(version)
    return f"{major}.{minor}.{max(patch_ + step, 0)}" if patch_ + step >= 0 else f"{major}.{minor - 1}.9"


NEWER, OLDER = bump(CURRENT, 1), bump(CURRENT, -1)


def wheel_bytes(version: str, requires: tuple[str, ...] = ('mcp==2.2.0; extra == "full"',)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        meta = [f"Metadata-Version: 2.4", "Name: project-observatory", f"Version: {version}",
                "Provides-Extra: full", *[f"Requires-Dist: {r}" for r in requires]]
        zf.writestr(f"project_observatory-{version}.dist-info/METADATA", "\n".join(meta) + "\n")
        zf.writestr("observatory/__init__.py", f'__version__ = "{version}"\n')
    return buf.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeGitHub:
    """Serves /repos/<slug>/releases/{latest,tags/vX} and the asset bytes."""

    def __init__(self):
        self.releases: dict[str, dict] = {}   # version -> spec
        self.latest: str | None = None
        self.requests: list[str] = []
        self.rate_limited = False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                if outer.rate_limited:
                    body = b'{"message":"API rate limit exceeded"}'
                    self.send_response(403); self.send_header("X-RateLimit-Remaining", "0")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                    return
                prefix = f"/repos/{REPO}/releases/"
                if self.path.startswith(prefix):
                    rest = self.path[len(prefix):]
                    version = outer.latest if rest == "latest" else rest[len("tags/v"):] if rest.startswith("tags/v") else None
                    spec = outer.releases.get(version or "")
                    if spec is None:
                        return self._send(404, b'{"message":"Not Found"}')
                    return self._send(200, json.dumps(outer.document(version)).encode())
                if self.path.startswith("/assets/"):
                    version, name = self.path[len("/assets/"):].split("/", 1)
                    data = outer.releases.get(version, {}).get("files", {}).get(name)
                    if data is None:
                        return self._send(404, b"missing")
                    return self._send(200, data)
                self._send(404, b"{}")

            def _send(self, code, body):
                self.send_response(code); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def publish(self, version: str, *, latest: bool = True, wheel: bytes | None = None,
                digest: str | None = None, sums_line: str | None = None, omit: tuple[str, ...] = (),
                no_digest: bool = False) -> bytes:
        data = wheel if wheel is not None else wheel_bytes(version)
        name = f"project_observatory-{version}-py3-none-any.whl"
        sums = (sums_line if sums_line is not None else f"{sha(data)}  {name}") + "\n"
        files = {name: data, "SHA256SUMS": sums.encode()}
        digests = {name: digest or sha(data), "SHA256SUMS": sha(sums.encode())}
        self.releases[version] = {"files": files, "digests": digests, "omit": omit, "no_digest": no_digest}
        if latest:
            self.latest = version
        return data

    def document(self, version: str) -> dict:
        spec = self.releases[version]
        assets = []
        for name, data in spec["files"].items():
            if name in spec["omit"]:
                continue
            row = {"name": name, "size": len(data),
                   "browser_download_url": f"{self.api}/assets/{version}/{name}"}
            if not spec["no_digest"]:
                row["digest"] = "sha256:" + spec["digests"][name]
            assets.append(row)
        return {"tag_name": f"v{version}", "draft": False, "prerelease": False,
                "html_url": f"https://example.com/releases/v{version}", "assets": assets}

    def close(self):
        self.server.shutdown(); self.server.server_close()


class FakeInstaller:
    def __init__(self, fail_on: set[str] = frozenset(), name="pip"):
        self.calls: list[tuple[str, bool]] = []
        self.fail_on, self.name = set(fail_on), name
        self.installed = CURRENT

    def available(self):
        return self.name

    def install(self, wheel: Path, *, force: bool, constraints=None):
        version = wheel.name.split("-")[1]
        self.calls.append((version, force))
        if version in self.fail_on:
            import engine_update
            raise engine_update.InstallFailed(f"synthetic installer failure for {version}")
        self.installed = version
        return [{"command": "install", "wheel": wheel.name}]


class FakeServices:
    def __init__(self, loaded=("tick", "server"), available=True, stop_fails=(), start_fails=()):
        self.state = {name: name in loaded for name in ("tick", "server")}
        self.calls: list[tuple[str, str]] = []
        self._available, self.stop_fails, self.start_fails = available, set(stop_fails), set(start_fails)
        self.plists: dict[str, Path] = {}

    def available(self):
        return self._available

    def managed(self):
        return [{"name": n, "label": f"org.example.{n}", "plist": str(self.plists[n])} for n in ("tick", "server")]

    def loaded(self, label):
        self.calls.append(("loaded", label))
        return self.state[label.rsplit(".", 1)[1]]

    def stop(self, label):
        name = label.rsplit(".", 1)[1]
        self.calls.append(("stop", name))
        if name in self.stop_fails:
            return False, "synthetic stop failure"
        self.state[name] = False
        return True, ""

    def start(self, plist):
        name = Path(plist).stem
        self.calls.append(("start", name))
        if name in self.start_fails:
            return False, "synthetic start failure"
        self.state[name] = True
        return True, ""


class FakeEngine:
    """The new release's own commands: `upgrade`, `version`, `doctor`, dependency versions."""

    def __init__(self, installer: FakeInstaller, home: Path, *, upgrade_rc=0, doctor_rc=0,
                 upgrade_writes=False, deps=None):
        self.installer, self.home = installer, home
        self.upgrade_rc, self.doctor_rc, self.upgrade_writes = upgrade_rc, doctor_rc, upgrade_writes
        self.deps = {"mcp": "2.2.0"} if deps is None else deps
        self.calls: list[str] = []

    def upgrade(self):
        self.calls.append("upgrade")
        if self.upgrade_writes:
            (self.home / "config" / "upgraded-by-new-release.json").write_text("{}\n")
        return self.upgrade_rc, "synthetic upgrade output"

    def version(self):
        self.calls.append("version")
        return self.installer.installed

    def distribution_version(self):
        return self.installer.installed

    def dependencies(self, names):
        self.calls.append("dependencies")
        return {n: self.deps.get(n) for n in names}

    def doctor(self):
        self.calls.append("doctor")
        return self.doctor_rc, "synthetic doctor output"


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(self.base / "user")}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        import engine_update
        self.mod = importlib.reload(engine_update)
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.close)
        self.gh.publish(CURRENT, latest=False)
        self.installer = FakeInstaller()
        self.services = FakeServices()
        for n in ("tick", "server"):
            plist = self.base / f"{n}.plist"
            plist.write_text("synthetic plist")
            self.services.plists[n] = plist
        self.engine = FakeEngine(self.installer, self.home)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def deps(self, **overrides):
        values = dict(fetcher=self.mod.Fetcher(attempts=1, sleep=lambda s: None),
                      installer=self.installer, services=self.services,
                      engine=lambda home: self.engine,
                      origin=lambda: {"kind": "wheel", "version": CURRENT},
                      idle_timeout=0)
        values.update(overrides)
        return self.mod.Dependencies(**values)

    def run_cli(self, *args, **overrides):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["--repository", REPO, "--api-url", self.gh.api, *args], self.deps(**overrides))
        return code, json.loads(out.getvalue())

    def tree(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in sorted(self.home.rglob("*"))
                if p.is_file() and "backups" not in p.parts and not p.name.endswith(".lock")
                and p.name != "update.jsonl"}

    # --- check and preview -------------------------------------------------

    def test_up_to_date_check_and_preview(self):
        self.gh.latest = CURRENT
        code, doc = self.run_cli("--check")
        self.assertEqual((code, doc["status"]), (0, "up-to-date"))
        self.assertEqual(doc["degraded"], [])
        code, doc = self.run_cli()
        self.assertEqual((code, doc["status"], doc["update_available"]), (0, "preview", False))
        self.assertEqual(self.installer.calls, [])

    def test_newer_release_is_reported_with_its_own_exit_code(self):
        self.gh.publish(NEWER)
        before = self.tree()
        code, doc = self.run_cli("--check")
        self.assertEqual(code, self.mod.EXIT_UPDATE_AVAILABLE)
        self.assertEqual((doc["status"], doc["current"], doc["target"]), ("update-available", CURRENT, NEWER))
        code, doc = self.run_cli()
        self.assertEqual((code, doc["status"], doc["target"], doc["update_available"]), (0, "preview", NEWER, True))
        self.assertTrue(any("stop" in s for s in doc["would"]))
        # The preview says where the snapshot really goes: with no passphrase it
        # stays in the workspace, unencrypted; with one, encrypted into the root.
        line = next(s for s in doc["would"] if s.startswith("snapshot"))
        self.assertIn("unencrypted", line)
        import backup_vault
        backup_vault.set_passphrase(self.home, "synthetic passphrase for a preview")
        _, doc = self.run_cli()
        line = next(s for s in doc["would"] if s.startswith("snapshot"))
        self.assertIn("encrypted", line)
        self.assertIn(str(backup_vault.root_info(self.home)["path"]), line)
        backup_vault.passphrase_file(self.home).unlink()
        self.assertEqual(self.tree(), before, "a preview writes nothing")
        self.assertEqual(self.services.calls, [], "a preview does not even ask launchd")

    def test_explicit_version_uses_the_tag_endpoint(self):
        self.gh.publish(NEWER, latest=False)
        self.gh.latest = CURRENT
        code, doc = self.run_cli("--check", "--version", NEWER)
        self.assertEqual((code, doc["target"]), (self.mod.EXIT_UPDATE_AVAILABLE, NEWER))
        self.assertIn(f"/repos/{REPO}/releases/tags/v{NEWER}", self.gh.requests)
        code, doc = self.run_cli("--check", "--version", "9.9.9")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("not found", doc["error"])

    def test_network_down_is_degraded_never_up_to_date(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.main(["--check", "--repository", REPO, "--api-url", f"http://127.0.0.1:{closed}"], self.deps())
        doc = json.loads(out.getvalue())
        self.assertEqual((code, doc["status"]), (self.mod.EXIT_UNDETERMINED, "degraded"))
        self.assertTrue(doc["degraded"])
        self.assertNotIn("up-to-date", json.dumps(doc))

    def test_rate_limit_is_degraded(self):
        self.gh.rate_limited = True
        code, doc = self.run_cli("--check")
        self.assertEqual((code, doc["status"]), (self.mod.EXIT_UNDETERMINED, "degraded"))
        self.assertIn("rate limit", doc["degraded"][0])

    def test_plain_http_to_a_remote_host_is_refused(self):
        with self.assertRaises(self.mod.UpdateError):
            self.mod.check_url("http://example.com/repos")
        self.mod.check_url("https://api.github.com")
        self.mod.check_url("http://127.0.0.1:9")
        with self.assertRaises(self.mod.UpdateError):
            self.mod.check_url("https://" + "user:pw" + "@example.com/")

    def test_missing_asset_cannot_be_installed(self):
        self.gh.publish(NEWER, omit=(f"project_observatory-{NEWER}-py3-none-any.whl",))
        code, doc = self.run_cli("--check")
        self.assertEqual((code, doc["status"]), (self.mod.EXIT_UNDETERMINED, "degraded"))
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("no project_observatory", doc["error"])
        self.assertEqual((self.installer.calls, self.services.calls), ([], []))

    # --- verification ----------------------------------------------------------

    def assert_refused_untouched(self, code, doc, words):
        self.assertEqual(code, self.mod.EXIT_REFUSED, doc)
        self.assertIn(words, doc["error"])
        self.assertEqual(self.installer.calls, [])
        self.assertFalse([c for c in self.services.calls if c[0] in ("stop", "start")])
        self.assertFalse((self.home / "backups/engine-releases").exists())

    def test_github_digest_mismatch_refuses_before_anything(self):
        self.gh.publish(NEWER, digest="0" * 64)
        code, doc = self.run_cli("--apply")
        self.assert_refused_untouched(code, doc, "GitHub asset digest")

    def test_sha256sums_mismatch_refuses_before_anything(self):
        self.gh.publish(NEWER, sums_line=f"{'1' * 64}  project_observatory-{NEWER}-py3-none-any.whl")
        code, doc = self.run_cli("--apply")
        self.assert_refused_untouched(code, doc, "SHA256SUMS")

    def test_release_without_published_digests_is_refused(self):
        self.gh.publish(NEWER, no_digest=True)
        code, doc = self.run_cli("--apply")
        self.assert_refused_untouched(code, doc, "digest")

    def test_wheel_whose_metadata_names_another_version_is_refused(self):
        self.gh.publish(NEWER, wheel=wheel_bytes("0.0.1"))
        code, doc = self.run_cli("--apply")
        self.assert_refused_untouched(code, doc, "metadata")

    # --- refusals ---------------------------------------------------------------

    def test_downgrade_is_refused_and_check_says_newer_installed(self):
        self.gh.publish(OLDER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("downgrade", doc["error"])
        code, doc = self.run_cli("--check")
        self.assertEqual((code, doc["status"]), (0, "newer-installed"))
        self.assertEqual(self.installer.calls, [])

    def test_same_version_needs_reinstall(self):
        self.gh.latest = CURRENT
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("--reinstall", doc["error"])
        code, doc = self.run_cli("--apply", "--reinstall")
        self.assertEqual(code, 0, doc)
        self.assertEqual(self.installer.calls, [(CURRENT, True)])

    def test_source_checkout_refuses_apply(self):
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply", origin=lambda: {"kind": "source", "why": "synthetic checkout"})
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("source", doc["error"])
        self.assertEqual(self.installer.calls, [])

    def test_no_rollback_wheel_refuses_unless_no_rollback(self):
        self.gh.releases.pop(CURRENT)
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("--no-rollback", doc["error"])
        self.assertEqual(self.installer.calls, [])
        code, doc = self.run_cli("--apply", "--no-rollback")
        self.assertEqual(code, 0, doc)
        self.assertFalse(doc["rollback_available"])

    # --- the transaction ------------------------------------------------------

    def test_success_stops_and_restarts_only_loaded_services(self):
        self.services = FakeServices(loaded=("tick",))
        for n in ("tick", "server"):
            self.services.plists[n] = self.base / f"{n}.plist"
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc["status"], "updated")
        self.assertEqual([c for c in self.services.calls if c[0] != "loaded"], [("stop", "tick"), ("start", "tick")])
        self.assertEqual(self.installer.calls, [(NEWER, False)])
        self.assertEqual(self.engine.calls[:1], ["upgrade"])
        self.assertIn("doctor", self.engine.calls)
        cached = sorted(p.name for p in (self.home / "backups/engine-releases").glob("*.whl"))
        self.assertEqual(cached, sorted({f"project_observatory-{CURRENT}-py3-none-any.whl",
                                         f"project_observatory-{NEWER}-py3-none-any.whl"}))
        snapshot = Path(doc["snapshot"]["snapshot"])
        self.assertTrue(snapshot.exists())
        rows = [json.loads(line) for line in (self.home / "store/logs/update.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["event"], "updated")
        self.assertTrue(all("at" in r and "event" in r for r in rows))

    def plant_plugin(self, plugin_id: str):
        claude = self.base / "user" / ".claude"
        (claude / "plugins").mkdir(parents=True, exist_ok=True)
        (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {plugin_id: [{"scope": "user", "version": "0.0.1"}]}}))
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {plugin_id: True}}))

    def test_next_step_leaves_a_launcher_managed_plugin_to_its_launcher(self):
        self.plant_plugin("observatory-log@passioncode")
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, 0, doc)
        self.assertIn("Restart Claude Code sessions", doc["next"])
        self.assertNotIn("agent install", doc["next"], "a second copy would fight the launcher")
        self.assertIn("passioncode launcher", doc["next"])

    def test_next_step_suggests_agent_install_for_the_engines_own_plugin(self):
        self.plant_plugin("observatory-log@observatory-log")
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, 0, doc)
        self.assertIn("full agent install", doc["next"])

    def test_next_step_without_a_plugin_only_restarts_sessions(self):
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc["next"], "Restart Claude Code sessions")

    def test_services_not_loaded_nothing_is_stopped(self):
        self.services.state = {"tick": False, "server": False}
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, 0, doc)
        self.assertFalse([c for c in self.services.calls if c[0] in ("stop", "start")])
        self.assertEqual(doc["services_stopped"], [])

    def test_writers_stopped_never_touches_services(self):
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply", "--writers-stopped")
        self.assertEqual(code, 0, doc)
        self.assertEqual(self.services.calls, [])

    def test_loaded_job_without_plist_refuses_before_stopping(self):
        self.services.plists["server"].unlink()
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIn("plist", doc["error"])
        self.assertFalse([c for c in self.services.calls if c[0] == "stop"])

    def test_install_failure_rolls_back_and_restores_services(self):
        self.installer.fail_on = {NEWER}
        self.gh.publish(NEWER)
        before = self.tree()
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_FAILED, doc)
        self.assertTrue(doc["rolled_back"])
        self.assertEqual(self.installer.calls, [(NEWER, False), (CURRENT, True)])
        self.assertEqual(self.installer.installed, CURRENT)
        self.assertEqual(self.services.state, {"tick": True, "server": True})
        self.assertEqual(self.engine.calls.count("upgrade"), 0)
        self.assertEqual(self.tree(), before)

    def test_upgrade_failure_rolls_back_and_restores_services(self):
        self.engine.upgrade_rc = 1
        self.gh.publish(NEWER)
        before = self.tree()
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_FAILED, doc)
        self.assertTrue(doc["rolled_back"])
        self.assertFalse(doc["workspace_restored"])
        self.assertEqual(self.installer.installed, CURRENT)
        self.assertEqual(self.services.state, {"tick": True, "server": True})
        self.assertEqual(self.tree(), before)

    def test_failure_after_a_committed_upgrade_restores_the_workspace(self):
        self.engine.upgrade_writes = True
        self.engine.doctor_rc = 2
        (self.home / "secrets/demo-slot").write_text("synthetic private value")
        self.gh.publish(NEWER)
        before = self.tree()
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_FAILED, doc)
        self.assertTrue(doc["rolled_back"])
        self.assertTrue(doc["workspace_restored"])
        self.assertEqual(self.installer.installed, CURRENT)
        self.assertEqual(self.tree(), before, "the pre-update state is back at the same path")
        kept = Path(doc["failed_workspace"])
        self.assertTrue((kept / "config/upgraded-by-new-release.json").exists())
        self.assertTrue((self.home / "backups/engine-releases").is_dir(), "backups travel with the home")
        self.assertEqual(self.services.state, {"tick": True, "server": True})

    def test_missing_dependency_after_install_rolls_back(self):
        self.engine.deps = {}
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_FAILED, doc)
        self.assertIn("mcp", doc["error"])
        self.assertEqual(self.installer.installed, CURRENT)

    def test_failed_rollback_needs_a_person(self):
        self.installer.fail_on = {NEWER, CURRENT}
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_NEEDS_PERSON, doc)
        self.assertFalse(doc["rolled_back"])
        self.assertTrue(doc["human_steps"])

    def test_services_that_do_not_restart_are_reported(self):
        self.services.start_fails = {"server"}
        self.gh.publish(NEWER)
        code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_SERVICES, doc)
        self.assertEqual(doc["status"], "updated")
        self.assertIn("server", json.dumps(doc["services_not_restarted"]))

    def test_cached_rollback_wheel_is_reused_without_network(self):
        self.gh.publish(NEWER)
        self.assertEqual(self.run_cli("--apply")[0], 0)
        newest = bump(NEWER, 1)
        self.gh.publish(newest)
        self.gh.releases.pop(NEWER)
        self.installer.fail_on = {newest}
        self.engine.installer.installed = NEWER
        with patch.object(self.mod.config, "VERSION", NEWER):
            code, doc = self.run_cli("--apply")
        self.assertEqual(code, self.mod.EXIT_FAILED, doc)
        self.assertTrue(doc["rolled_back"])
        self.assertEqual(self.installer.installed, NEWER)

    def test_log_carries_no_credentials(self):
        self.gh.publish(NEWER)
        self.run_cli("--apply")
        text = (self.home / "store/logs/update.jsonl").read_text()
        for word in ("token", "password", "Authorization"):
            self.assertNotIn(word, text)
        self.assertEqual(oct((self.home / "store/logs/update.jsonl").stat().st_mode & 0o777), "0o600")

    def test_no_workspace_installs_only(self):
        other = self.base / "empty-home"
        self.gh.publish(NEWER)
        with patch.dict(os.environ, {"OBSERVATORY_HOME": str(other)}):
            code, doc = self.run_cli("--apply")
        self.assertEqual(code, 0, doc)
        self.assertEqual(self.engine.calls.count("upgrade"), 0)
        self.assertFalse(other.exists())


class Parsing(unittest.TestCase):
    def setUp(self):
        import engine_update
        self.mod = engine_update

    def test_requirements_follow_the_full_extra(self):
        pins = self.mod.requirement_pins([
            'mcp==2.2.0; extra == "full"', "sqlite-vec (==0.1.9) ; extra == 'full'",
            'pytest>=8; extra == "dev"', "plain>=1.0"])
        names = {p["name"]: p for p in pins}
        self.assertEqual(sorted(names), ["mcp", "plain", "sqlite-vec"])
        self.assertEqual(names["mcp"]["pin"], "2.2.0")
        self.assertIsNone(names["plain"]["pin"])

    def test_sums_file_parsing(self):
        good = "a" * 64
        self.assertEqual(self.mod.parse_sums(f"{good}  x.whl\n{good} *y.whl\n\n"), {"x.whl": good, "y.whl": good})
        with self.assertRaises(self.mod.UpdateError):
            self.mod.parse_sums(f"{good}  x.whl\n{'b' * 64}  x.whl\n")

    def test_installer_output_redacts_index_credentials(self):
        self.assertNotIn("secret", self.mod.redact_tail("Looking in https://" + "user:secret" + "@index.example.com/simple"))


class RealSeams(unittest.TestCase):
    """The production installer, launchd and child-process adapters, driven by fake runners."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "ws"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": self.tmp.name,
                                           "PYTHONPATH": "/synthetic/checkout"}, clear=True)
        self.env.start()
        import paths
        importlib.reload(paths)
        import engine_update
        self.mod = importlib.reload(engine_update)
        self.calls = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def runner(self, code=0):
        def run(argv, **kw):
            self.calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, code, stdout='{"application": "1.2.3"}', stderr="boom")
        return run

    def test_installer_installs_the_full_extra_and_forces_on_reinstall(self):
        inst = self.mod.Installer(python="/synthetic/python", runner=self.runner())
        with patch.object(self.mod.importlib.util, "find_spec", return_value=object()):
            inst.install(Path("/w/x.whl"), force=True, constraints=Path("/w/c.txt"))
        (first, kw), (second, _) = self.calls
        self.assertEqual(first[:4], ["/synthetic/python", "-m", "pip", "install"])
        self.assertEqual(first[-3:], ["-c", "/w/c.txt", "/w/x.whl[full]"])
        self.assertIn("--force-reinstall", second)
        self.assertIn("--no-deps", second)
        self.assertNotIn("PYTHONPATH", kw["env"])

    def test_installer_failure_is_reported_without_index_credentials(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Looking in https://" + "u:hidden" + "@idx.example.com/")
        inst = self.mod.Installer(python="/synthetic/python", runner=run)
        with patch.object(self.mod.importlib.util, "find_spec", return_value=object()):
            with self.assertRaises(self.mod.InstallFailed) as caught:
                inst.install(Path("/w/x.whl"), force=False)
        self.assertNotIn("hidden", str(caught.exception))

    def test_uv_is_used_when_pip_is_absent(self):
        inst = self.mod.Installer(python="/synthetic/python", runner=self.runner())
        with patch.object(self.mod.importlib.util, "find_spec", return_value=None), \
                patch.object(self.mod.shutil, "which", return_value="/synthetic/uv"):
            self.assertEqual(inst.available(), "uv")
            inst.install(Path("/w/x.whl"), force=False)
        self.assertEqual(self.calls[0][0][:5], ["/synthetic/uv", "pip", "install", "--python", "/synthetic/python"])

    def test_new_engine_runs_the_installed_package_not_a_checkout(self):
        engine = self.mod.NewEngine(self.home, python="/synthetic/python", runner=self.runner())
        self.assertEqual(engine.version(), "1.2.3")
        argv, kw = self.calls[0]
        self.assertEqual(argv[:4], ["/synthetic/python", "-P", "-m", "observatory"])
        self.assertEqual(argv[4:8], ["--home", str(self.home), "full", "version"])
        self.assertNotIn("PYTHONPATH", kw["env"])
        self.assertNotEqual(kw["cwd"], str(ROOT))

    def test_launchd_helpers_name_only_this_workspace(self):
        from tools import install_launchd
        launch = importlib.reload(install_launchd)
        jobs = launch.managed_jobs()
        self.assertEqual([j["name"] for j in jobs], ["tick", "server"])
        self.assertTrue(all(j["label"] == launch.instance_label(j["name"]) for j in jobs))
        self.assertTrue(all(j["plist"].endswith(j["label"] + ".plist") for j in jobs))
        seen = []
        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(launch.subprocess, "run", run):
            self.assertTrue(launch.job_loaded(jobs[0]["label"]))
            self.assertEqual(launch.stop_job(jobs[0]["label"]), (True, ""))
            self.assertEqual(launch.start_job(jobs[0]["plist"]), (True, ""))
        uid = os.getuid()
        self.assertEqual(seen, [["launchctl", "print", f"gui/{uid}/{jobs[0]['label']}"],
                                ["launchctl", "bootout", f"gui/{uid}/{jobs[0]['label']}"],
                                ["launchctl", "bootstrap", f"gui/{uid}", jobs[0]["plist"]]])

    def test_default_release_source_is_the_plugin_repository(self):
        from tools import agent_plugin
        self.assertEqual(self.mod.release_source()[1], agent_plugin.REPOSITORY)
        with patch.dict(os.environ, {self.mod.REPOSITORY_ENV: REPO}):
            self.assertEqual(self.mod.release_source(), (self.mod.DEFAULT_API, REPO))

    def test_a_source_checkout_is_not_an_installed_wheel(self):
        self.assertNotEqual(self.mod.install_origin()["kind"], "wheel")


class CliRoute(unittest.TestCase):
    def test_workspace_command_is_routed_and_degrades_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = socket.socket(); s.bind(("127.0.0.1", 0)); closed = s.getsockname()[1]; s.close()
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp,
                   "OBSERVATORY_HOME": str(Path(tmp) / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
            p = subprocess.run([sys.executable, str(ROOT / "observatory.py"), "update", "--check",
                                "--repository", REPO, "--api-url", f"http://127.0.0.1:{closed}"],
                               capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
            self.assertEqual(p.returncode, 3, p.stderr)
            self.assertEqual(json.loads(p.stdout)["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
