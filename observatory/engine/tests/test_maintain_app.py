"""`full maintain app`: the app step of a maintenance pass, alone (LC-16 activation).

The Mac app runs it from a detached helper after it has quit, so a verified bundle that
waited with `waiting-for-quit` is swapped in at once. These tests drive the step with a
stand-in updater: nothing here runs codesign, pgrep or touches an installed app."""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import maintenance as M  # noqa: E402
import workspace  # noqa: E402

AT = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.timezone.utc)


class StubUpdater:
    """Records the state it was handed and answers like AppUpdater.step."""

    def __init__(self, result: dict | None = None, error: Exception | None = None):
        self.result = result or {"result": "updated", "from": "0.18.0", "version": "0.19.0"}
        self.error = error
        self.calls = 0

    def step(self, state: dict, at: datetime.datetime) -> dict:
        self.calls += 1
        if self.error:
            raise self.error
        state["app"] = dict(self.result, at=M.iso(at), target="0.19.0")
        return dict(self.result)


class MaintainApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home = base / "workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(base / "user"),
                                           "OBSERVATORY_SYSTEM_SETUP": "0"}, clear=True)
        self.env.start()
        workspace.initialize(self.home)
        M.write_state(self.home, {"pass": {"at": "2026-10-06T11:00:00Z"},
                                  "snapshot": {"at": "2026-10-06T11:00:00Z", "result": "ok"},
                                  "app": {"result": "waiting-for-quit", "pending": "0.19.0"}})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_only_the_app_step_runs_and_its_result_is_recorded(self):
        updater = StubUpdater()
        out = M.run_app_step(self.home, at=AT, app=updater)
        self.assertEqual(out["status"], "ran")
        self.assertEqual(out["app"]["result"], "updated")
        self.assertEqual(updater.calls, 1)
        state = M.read_state(self.home)
        self.assertEqual(state["app"]["result"], "updated")
        # Nothing else a pass does was touched: no update check, no snapshot, no new pass time.
        self.assertNotIn("update", state)
        self.assertEqual(state["snapshot"], {"at": "2026-10-06T11:00:00Z", "result": "ok"})
        self.assertEqual(state["pass"], {"at": "2026-10-06T11:00:00Z"})

    def test_a_running_pass_makes_it_busy_and_changes_nothing(self):
        updater = StubUpdater()
        with M.pass_lock(self.home) as held:
            self.assertTrue(held)
            out = M.run_app_step(self.home, at=AT, app=updater)
        self.assertEqual(out["status"], "busy")
        self.assertEqual(updater.calls, 0)
        self.assertEqual(M.read_state(self.home)["app"]["result"], "waiting-for-quit")

    def test_a_failing_step_is_recorded_not_raised(self):
        out = M.run_app_step(self.home, at=AT, app=StubUpdater(error=OSError("disk full")))
        self.assertEqual(out["app"]["result"], "error")
        self.assertIn("OSError", out["app"]["detail"])

    def test_the_cli_accepts_app_and_prints_the_answer(self):
        self.assertEqual(M.parser().parse_args(["app"]).action, "app")
        updater = StubUpdater({"result": "waiting-for-quit", "version": "0.18.0", "pending": "0.19.0"})
        buf, real = io.StringIO(), M.run_app_step
        # `maintain app` needs no system-setup consent: it touches neither launchd nor
        # the credential store (OBSERVATORY_SYSTEM_SETUP=0 here).
        with patch.object(M, "run_app_step", lambda base: real(base, at=AT, app=updater)), \
                contextlib.redirect_stdout(buf):
            code = M.main(["app"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["app"]["result"], "waiting-for-quit")

    @unittest.skipIf(sys.platform == "darwin", "the skip is for systems without the app")
    def test_without_macos_there_is_no_app_to_swap(self):
        self.assertEqual(M.run_app_step(self.home, at=AT)["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
