#!/usr/bin/env python3
"""OBS-610: every live writer declares the files it changes while an update stages.

Three updates rolled back because a writer nobody stops wrote a file the stillness check did not
excuse: the update-check job (2026-11-10), the analytics queue (2026-11-11) and the local server's
receipt (2026-11-17). Each is reproduced here with the REAL writer, against a real snapshot and a
real staged upgrade. Beside them: the declarations are the only spelling of their paths (a writer
imports its path, so it cannot drift from what the check excuses), a live writer writes nothing
it did not declare, and a change to any undeclared path still refuses and names it.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / "tools"))
import configuration as config  # noqa: E402
import runtime_receipts as rr  # noqa: E402
import workspace  # noqa: E402
import workspace_upgrade as upgrade  # noqa: E402

ALPHA = "m-machine-alpha-0001"
#: A well-formed App-Key no service knows (a fake value: nothing is ever sent from a test).
FAKE_APP_KEY = "A-XX-0000000000"
#: The marker that lets one line spell a declared name for another reason.
ALLOW = "runtime-receipts: allow"


def _engine_sources():
    for file in sorted(ROOT.rglob("*.py")):
        rel = file.relative_to(ROOT).as_posix()
        # `skill/` holds the plugin's hooks, which run without the engine on their path.
        if rel.startswith(("tests/", "docs/", "skill/")) or "/tests/" in rel or rel == "runtime_receipts.py":
            continue
        yield rel, file


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _joined_literals(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every `left / "literal"` path join: (line, the left side's source, the literal)."""
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)):
            out.append((node.lineno, ast.unparse(node.left), node.right.value))
    return out


def _workspace_path(left: str, literal: str) -> str | None:
    """The workspace-relative path a join builds, when its left side is a known runtime folder."""
    folders = {"paths.SCRATCH": "store/raw", "paths.STORE": "store", "paths.STATE": "store",
               "folder(base)": "store/analytics"}
    if left in folders:
        return f"{folders[left]}/{literal}"
    for tail, folder in (("/ 'store' / 'raw'", "store/raw"), ("/ 'store' / 'analytics'", "store/analytics"),
                         ("/ 'store'", "store")):
        if left.endswith(tail):
            return f"{folder}/{literal}"
    return None


class Declarations(unittest.TestCase):
    """The registry is well formed, and each writer takes its path from it."""

    def constant_names(self):
        return {id(value): key for key, value in vars(rr).items() if isinstance(value, rr.Receipt)}

    def test_every_declaration_is_a_workspace_file_with_an_owner_and_a_reason(self):
        seen = set()
        for receipt in rr.DECLARED:
            with self.subTest(receipt.path):
                self.assertTrue(receipt.path.startswith("store/"), "runtime files live under store/")
                self.assertNotIn(receipt.path, seen)
                seen.add(receipt.path)
                self.assertIn(receipt.writer, rr.WRITERS)
                self.assertTrue(receipt.why.strip())
                for owner in receipt.owners:
                    self.assertTrue((ROOT / owner).is_file(), owner)

    def test_each_writer_imports_its_path_from_the_registry(self):
        names = self.constant_names()
        for receipt in rr.DECLARED:
            constant = names[id(receipt)]
            for owner in receipt.owners:
                with self.subTest(receipt=receipt.path, owner=owner):
                    text = (ROOT / owner).read_text(encoding="utf-8")
                    self.assertIn("runtime_receipts", text)
                    self.assertIn(f"rr.{constant}", text, f"{owner} must take {receipt.path} from rr.{constant}")

    def test_no_module_spells_a_declared_path_itself(self):
        # A path spelled twice drifts: a writer renamed beside an unchanged excuse is the next rollback.
        names = {r.name for r in rr.DECLARED} | {r.path for r in rr.DECLARED}
        found = []
        for rel, file in _engine_sources():
            text = file.read_text(encoding="utf-8")
            tree = ast.parse(text)
            lines = text.splitlines()
            docs = _docstrings(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names
                        and id(node) not in docs and ALLOW not in lines[node.lineno - 1]):
                    found.append(f"{rel}:{node.lineno} {node.value!r}")
        self.assertEqual(found, [], "take the path from runtime_receipts instead")

    def test_a_live_writer_writes_nothing_it_did_not_declare(self):
        # A module that writes while an update stages and joins a runtime folder with a name the
        # registry does not declare (nor lists as a read) is the next rollback: declare it here.
        owners = sorted({o for r in rr.DECLARED for o in r.owners})
        undeclared = []
        for owner in owners:
            tree = ast.parse((ROOT / owner).read_text(encoding="utf-8"))
            for line, left, literal in _joined_literals(tree):
                path = _workspace_path(left, literal)
                if path is None or literal in {"store", "raw", "analytics", "logs"}:
                    continue
                if not rr.live(path) and path not in rr.HOLDS_STILL:
                    undeclared.append(f"{owner}:{line} {path}")
        self.assertEqual(undeclared, [])

    def test_the_static_scan_sees_a_new_undeclared_writer(self):
        # The scan above is only worth its silence if it fires: an undeclared receipt does.
        tree = ast.parse('LOG = paths.SCRATCH / "new-receipt.json"\nX = base / "store" / "update-check.json"\n')
        paths_built = [p for p in (_workspace_path(left, lit) for _, left, lit in _joined_literals(tree)) if p]
        self.assertEqual(paths_built, ["store/raw/new-receipt.json", "store/update-check.json"])
        self.assertFalse(rr.live("store/raw/new-receipt.json"))
        self.assertTrue(rr.live("store/update-check.json"))

    def test_what_is_live(self):
        for name, expected in (("store/raw/serverd.json", True), ("store/raw/.serverd.json.k2x9.tmp", True),
                               ("store/update-check.json", True), ("store/.update-check.json.ab12", True),
                               ("store/analytics/install-id", True),
                               ("store/analytics/.counters.json.12.34.tmp", True),
                               ("store/analytics/counters-problem.json", True),
                               ("store/logs/tick.log.3", True), ("store/logs/hooks.jsonl", True),
                               ("store/raw/tick.json", False), ("store/raw/.tick.json.k2x9.tmp", False),
                               ("store/analytics/other.json", False), ("store/maintenance.json", False),
                               ("config/serverd.json", False), ("config/settings.json", False),
                               ("secrets/demo-slot", False), ("registry/projects.json", False),
                               ("store/raw/serverd.json.bak", False), ("store/raw/x/serverd.json", False)):
            with self.subTest(name):
                self.assertEqual(rr.live(name), expected)

    def test_the_upgrades_views_are_the_registry(self):
        self.assertEqual(upgrade.SESSION_RECEIPTS, rr.by_writer(rr.SESSION_HOOK))
        self.assertEqual(upgrade.JOB_RECEIPTS, rr.by_writer(rr.CHECK_JOB))
        self.assertEqual(upgrade.ANALYTICS_RECEIPTS, rr.by_writer(rr.SESSION_MCP))
        self.assertEqual(upgrade.RUNTIME_RECEIPTS, rr.by_writer(rr.SERVER))


@unittest.skipIf(os.name == "nt", "snapshots and upgrades stop at a bad file descriptor on Windows, as the "
                 "workspace_upgrade suite does (tools/windows-known.json, OBS-278); the declarations above still run")
class LiveWriters(unittest.TestCase):
    """The real writers, run against a real workspace: everything they change is declared, and
    the three historical rollbacks no longer happen."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "private workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "OBSERVATORY_MACHINE_ID": ALPHA,
                                           "OBSERVATORY_SYSTEM_SETUP": "0",
                                           "OBSERVATORY_PRODUCT_LOG_DIR": str(self.base / "product-logs"),
                                           "OBSERVATORY_ANALYTICS_APP_KEY": FAKE_APP_KEY}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        from store import db
        importlib.reload(db)
        db.connect().close()
        (self.home / "store/raw").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # --- the real writers ------------------------------------------------------------------------

    def check_job(self):
        import maintenance
        maintenance.check_pass(self.home, feed=lambda base: "9.9.9", kick=lambda: {"result": "started"})

    def analytics(self):
        import usage_analytics as ua
        ua.install_id(self.home)
        ua.record("mcp_tool_called", self.home, surface="mcp", tool="observatory_overview", outcome="ok",
                  duration_ms=5)
        ua.mark_notice(self.home, "cli")
        ua.count(self.home, "refused")

    def server(self):
        import serverd
        with patch.object(serverd, "RECEIPT", rr.SERVERD.at(self.home / "store/raw")), \
                patch.object(serverd, "refresh_remote", return_value={}), \
                patch.object(serverd, "refresh_leaks", return_value={}), \
                patch.object(serverd, "refresh_skills", return_value={}), \
                patch.object(serverd, "_tick_health", return_value={}), \
                patch.object(serverd, "_LAST_RECEIPT", [None, 0.0]):
            serverd.heartbeat()

    def hooks(self):
        import companion_faults
        import why_asks
        import session_start
        raw = self.home / "store/raw"
        journal = rr.WHY_ASKS.at(raw)
        with patch.object(companion_faults, "LOG", rr.COMPANION_FAULTS.at(raw)), \
                patch.object(why_asks, "JOURNAL", journal), patch.object(why_asks, "PREVIOUS", rr.WHY_ASKS_PREVIOUS.at(raw)), \
                patch.object(why_asks, "ROTATE_BYTES", 10), patch.object(session_start, "SEEN", rr.SESSIONS_SEEN.at(raw)):
            companion_faults.append({"reason": "synthetic"})
            why_asks.ask_once("mem:synthetic-1")
            why_asks.ask_once("mem:synthetic-2")
            session_start.unknown_line(self.home, "", "session-1")

    def moved_by(self, writer) -> set[str]:
        before = upgrade.inventory(self.home)
        writer()
        after = upgrade.inventory(self.home)
        return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}

    def test_everything_a_live_writer_changes_is_declared(self):
        for name, writer in (("check job", self.check_job), ("analytics", self.analytics),
                             ("server", self.server), ("hooks", self.hooks)):
            with self.subTest(name):
                moved = self.moved_by(writer)
                self.assertTrue(moved, f"the {name} writer wrote something")
                self.assertEqual({m for m in moved if not rr.live(m)}, set())

    # --- the three rollbacks, reproduced ---------------------------------------------------------

    def during_snapshot(self, writer):
        real = workspace.copy_private

        def copy_while_writing(source, dest):
            real(source, dest)
            writer()
        with patch.object(workspace, "copy_private", side_effect=copy_while_writing):
            return Path(upgrade.snapshot(self.home, writers_stopped=True)["snapshot"])

    def while_staged(self, writer):
        real = upgrade.prepare_upgrade

        def staged_while_writing(stage):
            out = real(stage)
            writer()
            return out
        with patch.object(upgrade, "prepare_upgrade", side_effect=staged_while_writing):
            return upgrade.upgrade(self.home, apply=True, writers_stopped=True)

    def test_2026_11_10_the_check_job_writing_during_the_daily_snapshot(self):
        self.assertTrue((self.during_snapshot(self.check_job) / "manifest.json").exists())
        self.assertTrue(rr.UPDATE_CHECK.at(self.home / "store").is_file())

    def test_2026_11_11_a_sessions_analytics_while_an_update_stages(self):
        # A fresh workspace: the session's first event also makes the install id.
        self.assertEqual(self.while_staged(self.analytics)["status"], "upgraded")
        self.assertTrue(rr.ANALYTICS_INSTALL_ID.at(self.home / "store/analytics").is_file())

    def test_2026_11_17_a_reloaded_server_beating_while_an_update_stages(self):
        self.assertEqual(self.while_staged(self.server)["status"], "upgraded")
        doc = json.loads(rr.SERVERD.at(self.home / "store/raw").read_text())
        self.assertEqual(doc["workspace"], str(self.home))

    def test_the_session_hooks_while_an_update_stages(self):
        self.assertEqual(self.while_staged(self.hooks)["status"], "upgraded")

    def test_2026_11_17_a_server_loaded_while_the_update_holds_its_lock_waits(self):
        """OBS-593: launchd started a server of the new release mid-staging, loaded again by a process outside
        the engine. Whoever loads the job, the server it starts writes nothing while the update holds this
        workspace's lock, and starts once the update lets go: the staged upgrade holds still without the
        receipt's exemption, and the server is not left down."""
        import engine_update
        import serverd
        receipt = rr.SERVERD.at(self.home / "store/raw")
        started = threading.Event()

        def loaded_again():
            started.set()
            serverd.stand_down_while_updating(self.home, receipt.parent, poll=0.02, say=lambda line: None)
            self.server()
        server = threading.Thread(target=loaded_again, daemon=True)
        with engine_update.update_lock(self.home):
            out = self.while_staged(lambda: (server.start(), started.wait(5)))
            server.join(0.3)
            self.assertTrue(server.is_alive(), "the server waits while the update holds the lock")
            self.assertFalse(receipt.exists(), "and has written nothing")
        self.assertEqual(out["status"], "upgraded")
        server.join(10)
        self.assertFalse(server.is_alive(), "the lock let go, the server starts")
        self.assertEqual(json.loads(receipt.read_text())["workspace"], str(self.home))

    # --- the guarantee kept ----------------------------------------------------------------------

    def test_an_undeclared_change_still_refuses_and_is_named(self):
        def write(rel):
            def writer():
                file = self.home / rel
                file.parent.mkdir(parents=True, exist_ok=True)
                with open(file, "a") as out:
                    out.write(" ")      # still valid JSON, so the next case's preflight reads it
            return writer
        for rel in ("config/settings.json", "store/raw/tick.json", "store/analytics/other.json",
                    "store/maintenance.json", "secrets/demo-slot"):
            with self.subTest(rel):
                with self.assertRaises(config.ConfigurationError) as refused:
                    self.while_staged(write(rel))
                self.assertIn(rel, str(refused.exception))
                self.assertFalse((self.home / upgrade.JOURNAL).exists())
                with self.assertRaises(config.ConfigurationError) as refused:
                    self.during_snapshot(write(rel))
                self.assertIn(rel, str(refused.exception))

    def test_a_server_receipt_of_another_workspace_still_refuses(self):
        rr.SERVERD.at(self.home / "store/raw").write_text(json.dumps({"workspace": str(self.home)}))

        def foreign():
            rr.SERVERD.at(self.home / "store/raw").write_text(json.dumps({"workspace": str(self.base / "other")}))
        with self.assertRaises(config.ConfigurationError) as refused:
            self.while_staged(foreign)
        self.assertIn(rr.SERVERD.path, str(refused.exception))
        self.assertEqual(rr.foreign(self.home), {rr.SERVERD.path})


class TheServerStandsDownForAnUpdate(unittest.TestCase):
    """OBS-593: the gate at the top of `serverd.serve`, with the update lock's probe injected."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import serverd
        self.serverd = serverd
        self.home = Path("/opt/synthetic/workspace")

    def gate(self, answers, scratch=None):
        answers, sleeps, said = list(answers), [], []

        def held(home):
            self.assertEqual(home, self.home)
            value = answers.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        waited = self.serverd.stand_down_while_updating(
            self.home, scratch or self.home / "store/raw", held=held, sleep=sleeps.append, say=said.append, poll=2.0)
        return waited, sleeps, said, answers

    def test_it_waits_while_the_lock_is_held_and_says_so_once(self):
        waited, sleeps, said, left = self.gate([True, True, True, False])
        self.assertEqual((waited, sleeps, left), (6.0, [2.0, 2.0, 2.0], []))
        self.assertEqual(len(said), 1)
        self.assertIn("update", said[0])

    def test_it_starts_at_once_when_no_update_holds_the_lock(self):
        self.assertEqual(self.gate([False])[:3], (0.0, [], []))

    def test_a_probe_that_cannot_answer_does_not_keep_the_server_down(self):
        self.assertEqual(self.gate([OSError("unreadable")])[:3], (0.0, [], []))

    def test_a_receipt_written_outside_the_workspace_never_waits(self):
        # A server whose writes are redirected out of the home (a test's scratch) touches nothing an update stills.
        self.assertEqual(self.gate([], scratch=Path("/tmp/elsewhere/raw"))[:3], (0.0, [], []))

    def test_serve_stands_down_before_its_lock_its_token_or_its_socket(self):
        order = []

        class Stop(Exception):
            pass

        def lock(_where):
            order.append("lock")
            raise Stop
        with patch.object(self.serverd, "stand_down_while_updating", side_effect=lambda *a, **k: order.append("gate")), \
                patch.object(self.serverd.fs, "hold_single_instance", side_effect=lock):
            with self.assertRaises(Stop):
                self.serverd.serve(0)
        self.assertEqual(order, ["gate", "lock"])


if __name__ == "__main__":
    unittest.main()
