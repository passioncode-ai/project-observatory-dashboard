#!/usr/bin/env python3
"""The always-on server as a fabric-service/0.1 service.

Every rule the protocol puts on `tools/serverd.py` is checked here against a
synthetic workspace in a temporary directory: the instance lock before any side
effect, the well-known document answered from memory, the token-guarded events
feed as a view over the event store, the descriptor written only by the
installer, the manifest's extension key, and — end to end — the protocol's own
conformance probe (`tests/check_service.py`, vendored from the kit) against a
running server. Nothing here reads the operator's workspace, writes a real
LaunchAgent or calls `launchctl`.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMAS = ROOT / "fabric/service-schemas"
PROBE = ROOT / "tests/check_service.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def validator(name: str):
    import jsonschema
    from referencing import Registry, Resource
    registry = Registry()
    for f in sorted(SCHEMAS.glob("*.schema.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, registry=registry)


def schema_errors(name: str, doc) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(name).iter_errors(doc)]


def make_workspace(home: Path, *, scheduler: bool = False) -> None:
    (home / "config").mkdir(parents=True)
    (home / "registry").mkdir()
    (home / "store/raw").mkdir(parents=True)
    (home / "workspace.json").write_text(json.dumps(
        {"format_version": 1, "minimum_reader": "0.2.0", "minimum_writer": "0.2.0"}))
    (home / "config/settings.json").write_text(json.dumps(
        {"schema_version": 1, "sources": {}, "integrations": {}, "features": {"scheduler": scheduler}}))


def make_store(db: Path) -> sqlite3.Connection:
    """The real schema, so the view is tested against the table the collectors write."""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript((ROOT / "store/schema.sql").read_text(encoding="utf-8"))
    return conn


def add_event(conn, ident, kind, occurred_at, *, project=None, repo=None, ref=None, actor=None, payload=None):
    conn.execute("INSERT INTO events (id, project_id, repo_id, kind, ref, actor, occurred_at, payload_json)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (ident, project, repo, kind, ref, actor, occurred_at, json.dumps(payload or {})))
    conn.commit()


class Sandbox(unittest.TestCase):
    """A private HOME and workspace, with every OBSERVATORY_* override removed."""

    scheduler = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        make_workspace(self.home, scheduler=self.scheduler)
        self.services = self.base / "services"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(HOME=str(self.base / "user"), OBSERVATORY_HOME=str(self.home),
                   FABRIC_SERVICES_DIR=str(self.services), PYTHONDONTWRITEBYTECODE="1")
        (self.base / "user").mkdir()
        self.env = env
        self.patch = mock.patch.dict(os.environ, env, clear=True)
        self.patch.start()
        import paths
        importlib.reload(paths)
        self.paths = paths
        import service_identity, service_health, service_events
        self.si = importlib.reload(service_identity)
        self.health = importlib.reload(service_health)
        self.events = importlib.reload(service_events)
        from tools import serverd
        self.serverd = importlib.reload(serverd)
        import fabric_service
        self.fs = fabric_service

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()


# --- the vendored kit --------------------------------------------------------------

class VendoredKit(unittest.TestCase):
    def test_kit_and_probe_are_the_upstream_bytes(self):
        """The header records the upstream digest; an edit here would drift from the kit."""
        for path in (ROOT / "fabric_service.py", PROBE):
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            self.assertTrue(lines[0].startswith("# Vendored from passioncode-ai/fabric-agent-adapter"), path)
            recorded = lines[2].split("upstream sha256 ", 1)[1].split()[0]
            body = "".join(lines[4:]).encode("utf-8")
            self.assertEqual(hashlib.sha256(body).hexdigest(), recorded,
                             f"{path.name} differs from the kit it was copied from")

    def test_contract_schemas_are_bundled(self):
        names = {p.name for p in SCHEMAS.glob("*.schema.json")}
        self.assertEqual(names, {"common.schema.json", "service-common.schema.json",
                                 "service-descriptor.schema.json", "service-well-known.schema.json",
                                 "service-events-page.schema.json"})


# --- identity ------------------------------------------------------------------------

class Identity(Sandbox):
    def test_instance_is_default_only_for_the_standard_workspace(self):
        standard = self.base / "user/.local/share/project-observatory-full"
        self.assertEqual(self.si.instance(standard), "default")
        other = self.si.instance(self.home)
        self.assertRegex(other, r"^ws-[0-9a-f]{16}$")
        self.assertRegex(other, r"^[a-z][a-z0-9-]{0,31}$", "the protocol's instance pattern")
        from tools import install_launchd
        label = importlib.reload(install_launchd).instance_label("server")
        self.assertIn(other[3:], label, "one workspace: one label digest, one instance")

    def test_lock_and_token_are_in_the_workspace_and_ignored_by_its_history(self):
        self.assertEqual(self.si.lock_dir(), self.home)
        self.assertEqual(self.si.token_file(), self.home / "service.token")
        import workspace_upgrade
        self.assertTrue(workspace_upgrade.excluded(Path("service.lock")),
                        "a held lock is not state a snapshot should carry")

    def test_build_prefers_the_stated_commit(self):
        with mock.patch.dict(os.environ, {"FABRIC_BUILD_COMMIT": "ABCDEF1234"}):
            self.assertEqual(self.si.build(), {"commit": "abcdef1234"})

    def test_a_foreign_enclosing_repository_is_not_this_build(self):
        outer = self.base / "someone-elses-checkout"
        engine = outer / "vendor/engine"
        engine.mkdir(parents=True)
        (engine / "SOURCE-INVENTORY.json").write_text('{"files": []}')
        subprocess.run(["git", "init", "-q", str(outer)], check=True)
        subprocess.run(["git", "-C", str(outer), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
        got = self.si.build(engine)
        self.assertNotIn("commit", got, "the enclosing checkout's commit is not the engine's build")
        self.assertEqual(got["digest"], "sha256:" + hashlib.sha256(b'{"files": []}').hexdigest())

    def test_an_installed_wheel_reports_its_archive_digest(self):
        engine = self.base / "site/observatory/engine"
        engine.mkdir(parents=True)
        digest = "ab" * 32

        class Dist:
            def locate_file(self, rel):
                return self.root / rel
            def read_text(self, name):
                return {"direct_url.json": json.dumps({"archive_info": {"hashes": {"sha256": digest}}}),
                        "RECORD": "x"}.get(name)
        dist = Dist()
        dist.root = self.base / "site"
        with mock.patch("importlib.metadata.distribution", return_value=dist):
            self.assertEqual(self.si.build(engine), {"digest": "sha256:" + digest})
            dist.read_text = lambda name: {"direct_url.json": json.dumps(
                {"vcs_info": {"commit_id": "0123456789abcdef0123456789abcdef01234567"}})}.get(name)
            self.assertEqual(self.si.build(engine), {"commit": "0123456789ab"})
            dist.read_text = lambda name: {"RECORD": "a,b,c\n"}.get(name)
            self.assertEqual(self.si.build(engine),
                             {"digest": "sha256:" + hashlib.sha256(b"a,b,c\n").hexdigest()})

    def test_descriptor_is_valid_and_names_this_workspace(self):
        doc = self.si.descriptor(47311, label="org.example.server", plist=self.base / "x.plist")
        self.assertEqual(schema_errors("service-descriptor.schema.json", doc), [])
        self.assertEqual(self.fs.validate_descriptor(doc), [])
        self.assertEqual(doc["paths"]["data"], str(self.home))
        self.assertEqual(doc["auth"], {"tokenFile": str(self.home / "service.token")})
        self.assertEqual(doc["lifecycle"]["manager"], "launchd")
        self.assertTrue(Path(doc["fabricManifest"]).is_file())
        unmanaged = self.si.descriptor(47311)
        self.assertEqual(unmanaged["lifecycle"], {"manager": "none"})


# --- the well-known snapshot ------------------------------------------------------------

class Health(Sandbox):
    def snapshot(self, leaks=None):
        return self.health.snapshot(leaks or {"register": False, "open": 0, "total": 0})

    def tiles(self, snap):
        return {t["label"]: t for t in snap["summary"]}

    def test_a_fresh_workspace_is_healthy_and_says_nothing_was_built(self):
        snap = self.snapshot()
        self.assertEqual(snap["degraded"], [])
        tiles = self.tiles(snap)
        self.assertEqual(tiles["Projects"]["value"], "not built")
        self.assertEqual(tiles["Open findings"]["value"], "not built")
        self.assertEqual(tiles["Open leaks"]["value"], "not kept")
        self.assertEqual(tiles["Last tick"]["value"], "scheduler off")

    def test_counts_attention_and_what_needs_a_person(self):
        reg = self.home / "registry"
        (reg / "projects.json").write_text(json.dumps({"projects": [{"id": "project:a", "name": "A"},
                                                                    {"id": "project:b", "name": "B"}]}))
        (reg / "findings.json").write_text(json.dumps({"findings": [
            {"id": "f1", "severity": "critical"}, {"id": "f2", "severity": "warning"},
            {"id": "f3", "severity": "info"}, {"id": "f4", "severity": "warning", "acked": {"why": "known"}}]}))
        tiles = self.tiles(self.snapshot({"register": True, "readable": True, "open": 1, "total": 3}))
        self.assertEqual(tiles["Projects"], {"label": "Projects", "value": 2})
        self.assertEqual(tiles["Open findings"], {"label": "Open findings", "value": 2, "attention": True},
                         "critical + warning, not info, not acknowledged")
        self.assertEqual(tiles["Open leaks"], {"label": "Open leaks", "value": 1, "attention": True})

    def test_a_dead_tick_and_a_partial_collector_degrade_the_service(self):
        raw = self.home / "store/raw"
        (raw / "tick-lease.json").write_text(json.dumps({"last_acquired_at": "2026-09-01T10:00:00Z"}))
        (raw / "tick.json").write_text(json.dumps({"finished_at": "2026-09-01T09:00:00Z"}))
        (raw / "bitbucket.json").write_text(json.dumps({"degraded": [
            {"source": "workspace-a", "reason": "401"}, {"source": "workspace-b", "reason": "401"}]}))
        (self.home / "config/settings.json").write_text(json.dumps(
            {"schema_version": 1, "sources": {}, "integrations": {}, "features": {"scheduler": True}}))
        snap = self.snapshot()
        sources = {d["source"]: d["reason"] for d in snap["degraded"]}
        self.assertIn("tick", sources)
        self.assertTrue(sources["tick"].startswith("interrupted"))
        self.assertIn("collector:bitbucket", sources)
        self.assertIn("2 source(s)", sources["collector:bitbucket"])
        self.assertTrue(self.tiles(snap)["Last tick"]["attention"])

    def test_unreadable_sources_are_degraded_not_zero(self):
        (self.home / "registry/projects.json").write_text("{half a document")
        snap = self.snapshot({"register": True, "readable": False})
        sources = {d["source"] for d in snap["degraded"]}
        self.assertEqual(sources, {"registry", "leak-register"})
        self.assertEqual(self.tiles(snap)["Projects"]["value"], "unreadable")

    def test_limits_of_the_protocol_hold_under_many_sources(self):
        raw = self.home / "store/raw"
        for i in range(80):
            (raw / f"c{i:03d}.json").write_text(json.dumps({"degraded": [{"source": "s" * 200, "reason": "r" * 900}]}))
        snap = self.snapshot()
        self.assertLessEqual(len(snap["degraded"]), 64)
        self.assertTrue(all(len(d["source"]) <= 80 and len(d["reason"]) <= 300 for d in snap["degraded"]))
        self.assertIn("more degraded source", snap["degraded"][-1]["reason"])

    def test_a_registry_is_parsed_once_until_it_changes(self):
        f = self.home / "registry/projects.json"
        f.write_text(json.dumps({"projects": []}))
        self.snapshot()
        with mock.patch.object(self.health.json, "loads", side_effect=AssertionError("re-parsed")):
            self.snapshot()
        f.write_text(json.dumps({"projects": [{"id": "project:a"}]}))
        self.assertEqual(self.tiles(self.snapshot())["Projects"]["value"], 1)


# --- the events view -----------------------------------------------------------------

class Events(Sandbox):
    def setUp(self):
        super().setUp()
        self.db = self.home / "store/observatory.db"
        self.conn = make_store(self.db)
        self.labels = {"project:fabric": "Fabric", "project:site": "Site"}

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def page(self, after=None, limit=50):
        doc = self.events.page(self.db, after, limit, self.labels)
        self.assertEqual(schema_errors("service-events-page.schema.json", doc), [])
        return doc

    def commits(self, project, author, n, start=0):
        for i in range(n):
            add_event(self.conn, f"commit:{project}{start + i}", "commit", f"2026-09-28T10:{start + i:02d}:00+02:00",
                      project=project, repo="repository:owner/x", ref=f"{project}{start + i}", actor=author,
                      payload={"subject": f"change {start + i}", "repo": "owner/x"})

    def test_consecutive_commits_are_one_sentence(self):
        self.commits("project:fabric", "A. Author", 3)
        self.commits("project:site", "A. Author", 1, start=10)
        events = self.page()["events"]
        self.assertEqual(len(events), 2)
        first = events[0]
        self.assertEqual(first["kind"], "project.commits")
        self.assertTrue(first["text"].startswith("3 commits in Fabric by A. Author; latest “change 2”"), first["text"])
        self.assertEqual(first["at"], "2026-09-28T08:02:00Z", "the newest commit, in UTC")
        self.assertEqual(first["subject"], {"type": "project", "id": "project:fabric", "label": "Fabric"})
        self.assertEqual(first["link"], "/dashboard/projects.html#project:fabric")
        self.assertNotIn("notify", first)
        self.assertTrue(events[1]["text"].startswith("Commit in Site by A. Author: “change 10”"))

    def test_sessions_findings_and_unknown_kinds_read_as_sentences(self):
        add_event(self.conn, "session:1", "session", "2026-09-28T10:00:00.123Z", project="project:fabric",
                  ref="s1:project:fabric", actor="operator", payload={"prompts": 13})
        add_event(self.conn, "ev:notify:1", "finding.notified", "2026-09-28T11:00:00Z",
                  ref="clone.diverged:repository:owner/x@critical#0", actor="tool:notify_findings",
                  payload={"title": "owner/x has diverged from its remote", "severity": "critical", "delivered": True})
        add_event(self.conn, "ev:cleared:1", "finding.cleared", "2026-09-28T12:00:00Z",
                  ref="clone.diverged:repository:owner/x#0", actor="tool:notify_findings", payload={"episode": 0})
        add_event(self.conn, "x:1", "backup_run", "2026-09-28T13:00:00Z", project="project:site")
        session, opened, cleared, other = self.page()["events"]
        self.assertEqual(session["text"], "An agent session worked in Fabric (13 prompts).")
        self.assertEqual(opened["kind"], "finding.opened")
        self.assertEqual(opened["level"], "error")
        self.assertTrue(opened["notify"], "a newly opened finding asks the host to notify")
        self.assertEqual(opened["text"], "New critical finding: owner/x has diverged from its remote.")
        self.assertEqual(opened["link"], "/dashboard/findings.html#f-clone.diverged:repository:owner-x")
        self.assertEqual(cleared["text"], "Resolved: owner/x has diverged from its remote.")
        self.assertNotIn("notify", cleared)
        self.assertEqual(other["text"], "New backup run recorded for Site.")
        self.assertNotIn("backup_run", other["text"], "a machine id never reaches the sentence")

    def test_cursor_paging_sees_every_row_once_and_then_the_new_ones(self):
        self.commits("project:fabric", "A", 2)
        self.commits("project:site", "B", 2, start=10)
        self.commits("project:fabric", "C", 1, start=20)
        newest = self.page(limit=50)["events"]
        seen, cursor = [], "0"
        while True:
            doc = self.page(after=cursor, limit=1)
            if not doc["events"]:
                self.assertEqual(doc["cursor"], cursor, "an empty page keeps the cursor it was given")
                break
            seen += doc["events"]
            cursor = doc["cursor"]
        self.assertEqual([e["id"] for e in seen], [e["id"] for e in newest])
        self.assertEqual(seen, sorted(seen, key=lambda e: e["id"]))
        self.assertEqual(len({e["id"] for e in seen}), 3)
        self.commits("project:site", "D", 1, start=30)
        doc = self.page(after=cursor)
        self.assertEqual(len(doc["events"]), 1)
        self.assertGreater(doc["events"][0]["id"], cursor, "ids increase as strings too")

    def test_newest_limit_without_a_cursor_and_the_bounds(self):
        for i in range(5):
            self.commits(f"project:p{i}", "A", 1, start=i)
        doc = self.page(limit=2)
        self.assertEqual([e["subject"]["id"] for e in doc["events"]], ["project:p3", "project:p4"])
        self.assertEqual(self.fs.parse_limit("999"), 200)
        with self.assertRaises(self.fs.ServiceError):
            self.events.page(self.db, "not-a-cursor", 10, self.labels)
        self.assertEqual(len(self.page(after="0", limit=200)["events"]), 5,
                         "a cursor older than retention returns the oldest page that remains")

    def test_a_missing_store_is_an_empty_feed_and_a_broken_one_an_error(self):
        self.assertEqual(self.events.page(self.base / "absent.db", None, 10, {}), {"events": [], "cursor": None})
        broken = self.base / "broken.db"
        broken.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(self.events.FeedError):
            self.events.page(broken, None, 10, {})

    def test_the_view_never_writes(self):
        self.commits("project:fabric", "A", 1)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.page()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)


# --- startup order -------------------------------------------------------------------

class StartupOrder(Sandbox):
    def test_a_held_lock_stops_the_server_before_any_side_effect(self):
        calls = []
        with mock.patch.object(self.serverd.fs, "hold_single_instance", side_effect=SystemExit(75)), \
                mock.patch.object(self.serverd.fs, "ensure_token", side_effect=lambda p: calls.append("token")), \
                mock.patch.object(self.serverd, "heartbeat", side_effect=lambda: calls.append("heartbeat")), \
                mock.patch.object(self.serverd.http.server, "ThreadingHTTPServer",
                                  side_effect=lambda *a, **k: calls.append("bind")):
            with self.assertRaises(SystemExit) as stop:
                self.serverd.serve(free_port())
        self.assertEqual(stop.exception.code, 75)
        self.assertEqual(calls, [], "nothing may happen before the lock")
        self.assertFalse((self.home / "service.token").exists())

    def test_the_lock_comes_first_then_the_token_then_the_socket(self):
        calls = []

        class Lock:
            def release(self):
                calls.append("release")

        class Server:
            def __init__(self, *a, **k):
                calls.append("bind")
            def serve_forever(self):
                calls.append("serve")
            def server_close(self):
                pass
            def shutdown(self):
                pass
        with mock.patch.object(self.serverd.fs, "hold_single_instance",
                               side_effect=lambda d: calls.append("lock") or Lock()), \
                mock.patch.object(self.serverd.fs, "ensure_token",
                                  side_effect=lambda p: calls.append("token") or "t" * 43), \
                mock.patch.object(self.serverd, "heartbeat", side_effect=lambda: {"leaks": {}}), \
                mock.patch.object(self.serverd.http.server, "ThreadingHTTPServer", Server), \
                mock.patch.object(self.serverd.signal, "signal"):
            self.assertEqual(self.serverd.serve(free_port()), 0)
        self.assertEqual(calls[:4], ["lock", "token", "bind", "serve"])
        self.assertEqual(calls[-1], "release")


class CheapWellKnown(Sandbox):
    """The well-known route serves the snapshot; it never recomputes /health."""

    def test_the_route_reads_memory_only(self):
        import threading
        srv = self.serverd.http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.serverd.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.serverd.RUNTIME = self.serverd.Runtime("t" * 43)
            self.assertEqual(json.loads(get(port, "/.well-known/fabric-service")[2])["status"], "starting")
            self.serverd.RUNTIME.refresh({"register": False})
            with mock.patch.object(self.serverd, "heartbeat", side_effect=AssertionError("recomputed")) as beat, \
                    mock.patch.object(self.serverd, "refresh_leaks", side_effect=AssertionError("recomputed")), \
                    mock.patch.object(self.health, "snapshot", side_effect=AssertionError("recomputed")):
                for _ in range(3):
                    code, _h, body = get(port, "/.well-known/fabric-service")
                    self.assertEqual(code, 200, body)
                beat.assert_not_called()
            self.assertEqual(json.loads(body)["status"], "ready")
            self.serverd.RUNTIME.stopping()
            self.assertEqual(json.loads(get(port, "/.well-known/fabric-service")[2])["status"], "stopping")
        finally:
            self.serverd.RUNTIME = None
            srv.shutdown()
            srv.server_close()

    def test_a_bad_token_file_costs_the_feed_not_the_server(self):
        rt = self.serverd.Runtime(None, "Token file is readable by others; set mode 0600.")
        doc = rt.well_known()
        self.assertEqual(doc["status"], "starting")
        self.assertEqual([d["source"] for d in doc["degraded"]], ["service-token"])
        rt.refresh({"register": False})
        doc = rt.well_known()
        self.assertEqual(doc["status"], "degraded", "ready with a degraded source is not ready")
        self.assertEqual(schema_errors("service-well-known.schema.json", doc), [])


class BeatFailures(Sandbox):
    """A failing heartbeat or health snapshot becomes a degraded source, never an endless `starting`.

    Found 2026-09-29: the live server's heartbeat raised ENOSPC on a full disk, and on a
    macOS CI runner the first beat never produced a snapshot, so the well-known document
    said `starting` for good because refresh() only ran after a successful heartbeat()."""

    def test_a_heartbeat_that_raises_still_refreshes_and_names_the_failure(self):
        rt = self.serverd.Runtime("t" * 43)
        with mock.patch.object(self.serverd, "heartbeat", side_effect=OSError(28, "No space left on device")):
            self.serverd.beat_once(rt)
        doc = rt.well_known()
        self.assertNotEqual(doc["status"], "starting")
        self.assertIn("heartbeat", [d["source"] for d in doc["degraded"]])
        self.assertEqual(schema_errors("service-well-known.schema.json", doc), [])
        with mock.patch.object(self.serverd, "heartbeat", return_value={"leaks": {"register": False}}):
            self.serverd.beat_once(rt)
        self.assertNotIn("heartbeat", [d["source"] for d in rt.well_known()["degraded"]], "a recovered heartbeat clears its row")

    def test_a_health_snapshot_that_raises_is_degraded_not_starting(self):
        rt = self.serverd.Runtime("t" * 43)
        with mock.patch.object(self.serverd, "heartbeat", return_value={"leaks": {"register": False}}), \
                mock.patch.object(self.health, "snapshot", side_effect=RuntimeError("store unreadable")):
            self.serverd.beat_once(rt)
        doc = rt.well_known()
        self.assertEqual(doc["status"], "degraded")
        self.assertIn("health", [d["source"] for d in doc["degraded"]])
        self.assertEqual(schema_errors("service-well-known.schema.json", doc), [])


# --- installer -----------------------------------------------------------------------

class Installer(Sandbox):
    scheduler = True

    def install(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(self.serverd.fs, "launchd_install",
                                  side_effect=lambda label, plist, data, **kw: Path(plist).write_bytes(data)) as run:
            code = self.serverd.install()
        return code, run

    def test_install_writes_the_descriptor_then_the_plist(self):
        code, run = self.install()
        self.assertEqual(code, 0)
        path = self.services / f"project-observatory.{self.si.instance()}.json"
        doc = json.loads(path.read_text())
        self.assertEqual(schema_errors("service-descriptor.schema.json", doc), [])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(doc["lifecycle"], {"manager": "launchd", "label": self.serverd.LABEL,
                                            "plist": str(self.serverd.PLIST)})
        self.assertEqual(doc["origin"], f"http://127.0.0.1:{self.serverd.PORT}")
        kw = run.call_args.kwargs
        self.assertEqual((kw["service_id"], kw["instance"]), ("project-observatory", self.si.instance()))
        plist = plistlib.loads(self.serverd.PLIST.read_bytes())
        self.assertEqual((plist["RunAtLoad"], plist["KeepAlive"], plist["ThrottleInterval"]), (True, True, 10))
        self.assertGreater(plist["ExitTimeOut"], self.serverd.EXIT_TIMEOUT)
        self.assertEqual(os.stat(self.serverd.PLIST).st_mode & 0o777, 0o600)
        token = self.home / "service.token"
        self.assertNotIn(b"service.token", self.serverd.PLIST.read_bytes(), "the plist names no token")
        self.assertFalse(token.exists(), "the installer never creates the token; the service does, after its lock")

    def test_a_claimed_port_is_refused_before_any_plist(self):
        self.services.mkdir()
        other = {"protocol": "fabric-service/0.1", "id": "another-service", "instance": "default",
                 "name": "Another", "origin": f"http://127.0.0.1:{self.serverd.PORT}",
                 "auth": {"tokenFile": "~/x.token"}, "lifecycle": {"manager": "none"},
                 "paths": {"data": "~/x", "logs": []}, "installedAt": "2026-09-28T00:00:00Z",
                 "installedBy": "fixture"}
        (self.services / "another-service.default.json").write_text(json.dumps(other))
        code, run = self.install()
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertFalse(self.serverd.PLIST.exists(), "a refused claim leaves no plist behind")
        self.assertFalse((self.services / f"project-observatory.{self.si.instance()}.json").exists())

    def test_uninstall_removes_the_plist_and_the_descriptor_and_keeps_the_data(self):
        self.install()
        loaded = iter([True, True, False, False])
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(self.serverd.fs, "_launchctl") as launchctl, \
                mock.patch.object(self.serverd.fs, "launchd_loaded", side_effect=lambda label: next(loaded)) as probe:
            self.assertEqual(self.serverd.uninstall(), 0)
        self.assertEqual(launchctl.call_args_list[0].args[0], "bootout")
        self.assertEqual(probe.call_count, 4, "uninstall waits until launchd no longer lists the job")
        self.assertFalse(self.serverd.PLIST.exists())
        self.assertEqual(list(self.services.glob("*.json")), [])
        self.assertTrue((self.home / "workspace.json").exists())

    def test_install_refuses_off_macos_without_writing(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(self.serverd.install(), 1)
        self.assertFalse(self.services.exists())


# --- the running server --------------------------------------------------------------

def sandbox_env(base: Path, home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
    env.update(HOME=str(base / "user"), OBSERVATORY_HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


def start_server(env: dict, port: int, **pipes) -> subprocess.Popen:
    pipes = pipes or {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    return subprocess.Popen([sys.executable, str(ROOT / "tools/serverd.py"), "--run", "--port", str(port)],
                            cwd=ROOT, env=env, **pipes)


def get(port: int, path: str, headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": f"127.0.0.1:{port}"}
    h.update(headers or {})
    try:
        c.request("GET", path, headers=h)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 20) -> None:
    """Until the well-known document answers with a status past `starting`."""
    deadline = time.monotonic() + timeout
    last = "no answer yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise AssertionError(f"server exited {proc.returncode}: {err}")
        try:
            code, _h, body = get(port, "/.well-known/fabric-service")
            if code == 200 and json.loads(body)["status"] != "starting":
                return
            last = f"HTTP {code}: {body[:600].decode(errors='replace')}"
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.1)
    # A timeout on a slow runner must say where the server was, not only that it was late.
    output = _drain(proc)
    raise AssertionError(f"server did not become ready in {timeout:g}s; last answer: {last}; "
                         f"server output: {output}")


def _drain(proc: subprocess.Popen) -> str:
    """Stop the server and return the tail of what it printed (pipes only)."""
    if proc.poll() is None:
        proc.terminate()
    try:
        out, err = proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError):
        proc.kill()
        return "(no output: the server did not exit)"
    text = ((out or b"") + (err or b"")).decode(errors="replace")
    return text[-1500:] or "(nothing)"


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(10)
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()


class RunningServer(unittest.TestCase):
    """One real `serverd.py --run` for the class, as launchd would start it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.home = cls.base / "workspace"
        make_workspace(cls.home)
        (cls.base / "user").mkdir()
        cls.services = cls.base / "services"
        cls.env = sandbox_env(cls.base, cls.home, FABRIC_SERVICES_DIR=str(cls.services), PYTHONPATH=str(ROOT))
        conn = make_store(cls.home / "store/observatory.db")
        add_event(conn, "commit:1", "commit", "2026-09-28T10:00:00Z", project="project:fabric",
                  ref="1", actor="A. Author", payload={"subject": "first change"})
        conn.close()
        (cls.home / "registry/projects.json").write_text(json.dumps({"projects": [{"id": "project:fabric",
                                                                                    "name": "Fabric"}]}))
        cls.port = free_port()
        cls.proc = start_server(cls.env, cls.port)
        try:
            wait_ready(cls.port, cls.proc)
        except BaseException:
            stop_server(cls.proc)
            cls.tmp.cleanup()
            raise
        cls.token = (cls.home / "service.token").read_text().strip()

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.proc)
        cls.tmp.cleanup()

    def bearer(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def test_well_known_is_valid_fast_and_names_this_process(self):
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            code, headers, body = get(self.port, "/.well-known/fabric-service")
            timings.append((time.perf_counter() - started) * 1000)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        doc = json.loads(body)
        self.assertEqual(schema_errors("service-well-known.schema.json", doc), [])
        self.assertEqual(doc["service"]["id"], "project-observatory")
        self.assertRegex(doc["service"]["instance"], r"^ws-[0-9a-f]{16}$")
        self.assertEqual(doc["process"]["pid"], self.proc.pid)
        self.assertTrue(doc["service"]["build"].get("commit") or doc["service"]["build"].get("digest"))
        self.assertEqual(doc["surfaces"], {"dashboard": {"path": "/", "login": False},
                                           "events": {"path": "/fabric/v1/events"}})
        self.assertEqual({t["label"]: t["value"] for t in doc["summary"]}["Projects"], 1)
        self.assertLess(sorted(timings)[2], 100, f"median {sorted(timings)[2]:.1f} ms")

    def test_health_is_unchanged(self):
        code, _h, body = get(self.port, "/health")
        self.assertEqual(code, 200)
        self.assertEqual(set(json.loads(body)), {"at", "pid", "port", "version", "workspace", "uptime_s",
                                                 "remote", "leaks", "skills", "tick"})

    def test_the_guard_covers_the_protocol_routes(self):
        for path in ("/.well-known/fabric-service", "/fabric/v1/events"):
            for headers in ({"Host": "evil.example"}, {"Origin": "http://evil.example"},
                            {"Sec-Fetch-Site": "cross-site"}):
                self.assertEqual(get(self.port, path, headers)[0], 403, (path, headers))

    def test_events_need_the_token_in_the_header_and_only_there(self):
        code, headers, _b = get(self.port, "/fabric/v1/events")
        self.assertEqual(code, 401)
        self.assertEqual(headers.get("www-authenticate"), "Bearer")
        self.assertEqual(get(self.port, "/fabric/v1/events", self.bearer("x" * 43))[0], 401)
        self.assertEqual(get(self.port, "/fabric/v1/events?token=" + self.token)[0], 401,
                         "a token in the query string is not accepted")
        self.assertEqual(get(self.port, "/fabric/v1/events", {"Authorization": self.token})[0], 401,
                         "the scheme is Bearer")
        self.assertEqual(os.stat(self.home / "service.token").st_mode & 0o777, 0o600)

    def test_events_page_with_the_token(self):
        code, _h, body = get(self.port, "/fabric/v1/events?limit=5", self.bearer())
        self.assertEqual(code, 200)
        page = json.loads(body)
        self.assertEqual(schema_errors("service-events-page.schema.json", page), [])
        self.assertEqual(page["events"][0]["text"], "Commit in Fabric by A. Author: “first change”.")
        code, _h, body = get(self.port, "/fabric/v1/events?after=" + page["cursor"], self.bearer())
        self.assertEqual(json.loads(body), {"events": [], "cursor": page["cursor"]})
        self.assertEqual(get(self.port, "/fabric/v1/events?after=abc", self.bearer())[0], 400)

    def test_a_second_copy_exits_75_naming_the_holder_and_leaves_nothing(self):
        receipt = self.home / "store/raw/serverd.json"
        before = json.loads(receipt.read_bytes())["pid"]
        port = free_port()
        second = subprocess.run([sys.executable, str(ROOT / "tools/serverd.py"), "--run", "--port", str(port)],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 75, second.stderr)
        self.assertIn(f"process {self.proc.pid}", second.stderr)
        self.assertEqual(len(second.stderr.strip().splitlines()), 1, "one sentence")
        self.assertIsNone(self.proc.poll(), "the first copy keeps serving")
        self.assertEqual(json.loads(receipt.read_bytes())["pid"], before, "the second copy wrote no heartbeat")
        with self.assertRaises(OSError, msg="the second copy never bound its port"):
            get(port, "/health")

    def test_the_conformance_probe_passes(self):
        import fabric_service as fs
        import service_identity
        with mock.patch.dict(os.environ, self.env, clear=True):
            import paths
            importlib.reload(paths)
            importlib.reload(service_identity)
            fs.write_descriptor(service_identity.descriptor(self.port))
            target = f"project-observatory.{service_identity.instance()}"
        out = subprocess.run([sys.executable, str(PROBE), target, "--json"], cwd=ROOT, env=self.env,
                             capture_output=True, text=True, timeout=60)
        report = json.loads(out.stdout)
        verdicts = {r["rule"]: r for r in report["results"]}
        failed = {k: v["evidence"] for k, v in verdicts.items() if v["verdict"] == "FAIL"}
        self.assertEqual(failed, {})
        self.assertEqual(out.returncode, 0)
        not_run = {k for k, v in verdicts.items() if v["verdict"] == "NOT_RUN"}
        self.assertEqual(not_run, {"login.single-use", "lifecycle.launchd"},
                         "the dashboard declares no login and this descriptor has no supervisor")
        self.assertEqual(verdicts["lifecycle.instance-lock"]["verdict"], "PASS")
        self.assertEqual(verdicts["events.page"]["verdict"], "PASS")
        # The probe's own table, kept in the log as the receipt. Prefixed so the
        # portable runner does not count its rows as this suite's assertions.
        print("\n" + "\n".join(f"probe| {r['verdict']:8s} {r['rule']:31s} {r['evidence']}"
                                for r in report["results"]))


class Shutdown(unittest.TestCase):
    def test_sigterm_drains_and_releases_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            home = base / "workspace"
            make_workspace(home)
            (base / "user").mkdir()
            env = sandbox_env(base, home)
            port = free_port()
            proc = start_server(env, port)
            try:
                wait_ready(port, proc)
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(15), 0, "SIGTERM is a clean exit, not a kill")
                again = start_server(env, port)
                try:
                    wait_ready(port, again)
                finally:
                    stop_server(again)
            finally:
                stop_server(proc)


class DashboardOpen(Sandbox):
    def test_a_workspace_already_served_names_its_address(self):
        port = free_port()
        proc = start_server(self.env, port, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            wait_ready(port, proc)
            from tools import dashboard_open
            dashboard_open = importlib.reload(dashboard_open)
            import configuration
            with self.assertRaises(configuration.ConfigurationError) as refused:
                dashboard_open.start_server(free_port(), wait=20)
            self.assertIn(f"http://127.0.0.1:{port}/", str(refused.exception))
        finally:
            stop_server(proc)


# --- the provider manifest -------------------------------------------------------------

class Manifest(unittest.TestCase):
    def test_the_manifest_points_at_the_standard_descriptor(self):
        import service_identity
        import fabric_service as fs
        doc = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
        ext = doc["provider"]["extensions"][fs.EXTENSION_KEY]
        self.assertEqual(ext, {"descriptor": f"{service_identity.SERVICE_ID}.{service_identity.DEFAULT_INSTANCE}"})
        from tools import fabric_hash
        self.assertEqual(doc["provider"]["contentHash"], fabric_hash.compute(doc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
