"""Synthetic stage receipt failures; no collectors or operational workspace."""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def load_tick():
    spec = importlib.util.spec_from_file_location("stage_tick", ROOT / "tools/tick.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class StageReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="observatory-stage-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {"HOME": str(self.base), "OBSERVATORY_HOME": str(self.base / "workspace"),
                          "OBSERVATORY_TICK_RUN_ID": "a" * 32})
        self.env.start(); self.addCleanup(self.env.stop)
        self.tick = load_tick()
        self.scratch = self.base / "raw"; self.scratch.mkdir()
        self.t = self.tick.Tick(self.scratch)

    def read(self):
        return json.loads((self.scratch / "tick.json").read_text())

    def test_silent_tail_records_start_before_command_and_preserves_last_completion(self):
        prior = {"finished_at": "2026-01-01T00:00:00Z", "failed_steps": [{"step": "merge", "exit": 7}]}
        (self.scratch / "tick.json").write_text(json.dumps(prior))
        self.t = self.tick.Tick(self.scratch)
        def run(*args, **kwargs):
            doc = self.read(); row = doc["progress"]["stages"][-1]
            self.assertEqual(doc["finished_at"], prior["finished_at"])
            self.assertEqual(doc["failed_steps"], prior["failed_steps"])
            self.assertEqual((row["step"], row["outcome"], row["exit"]), ("merge", "running", None))
            return subprocess.CompletedProcess([], 7, "secret-child-output", "secret-child-error")
        with patch.object(self.tick.subprocess, "run", side_effect=run):
            self.assertEqual(self.t.bounded("merge", "synthetic"), (7, "secret-child-outputsecret-child-error"))
        row = self.read()["progress"]["stages"][-1]
        self.assertEqual((row["exit"], row["outcome"]), (7, "failed"))
        self.assertGreaterEqual(row["duration_seconds"], 0)
        self.assertIsNotNone(row["ended_at"])
        self.assertNotIn("secret-child", json.dumps(self.read()))

    def test_watchdog_not_started_and_timeout_are_distinct(self):
        for output, outcome in (("not started: no budget", "not_started"), ("time limit reached", "stopped")):
            with patch.object(self.tick.subprocess, "run", return_value=subprocess.CompletedProcess([],124,output,"")):
                self.t.bounded("merge", "synthetic")
            self.assertEqual(self.read()["progress"]["stages"][-1]["outcome"], outcome)

    def test_spawn_failure_records_unknown_without_copying_exception(self):
        with patch.object(self.tick.subprocess, "run", side_effect=OSError("secret-exception")):
            with self.assertRaises(OSError): self.t.bounded("merge", "synthetic")
        row=self.read()["progress"]["stages"][-1]
        self.assertEqual(row["outcome"], "unknown"); self.assertIsNone(row["exit"])
        self.assertNotIn("secret-exception", json.dumps(self.read()))

    def test_streaming_step_keeps_failure_and_has_stage_receipt(self):
        class Process:
            stdout = iter(["synthetic line\n"])
            def wait(self): return 9
        with patch.object(self.tick.subprocess, "Popen", return_value=Process()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.t.step("integrity", "synthetic"), "failed")
        self.assertEqual(self.t.failed, [("integrity", 9)])
        self.assertEqual(self.read()["progress"]["stages"][-1]["exit"], 9)

    def test_lost_end_receipt_never_invents_success(self):
        import atomic, tick_progress
        original=atomic.write_json
        def write(path, doc, **kwargs):
            if doc.get("progress", {}).get("stages", [{}])[-1].get("outcome") != "running":
                raise OSError("secret-storage-error")
            return original(path, doc, **kwargs)
        with patch.object(atomic, "write_json", side_effect=write), patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.t.bounded("merge", "synthetic")[0], 0)
        result=tick_progress.snapshot(self.scratch, "a"*32)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["stages"][-1]["outcome"], "unknown")
        self.assertIsNone(result["stages"][-1]["exit"])

    def test_stale_or_malformed_progress_is_unknown(self):
        import tick_progress
        with patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")):
            self.t.bounded("merge", "synthetic")
        self.assertEqual(tick_progress.snapshot(self.scratch, "b"*32)["coverage"], "unknown")
        doc=self.read(); doc["progress"]["stages"][-1]["step"]="secret-stage"
        (self.scratch/"tick.json").write_text(json.dumps(doc))
        snapshot=tick_progress.snapshot(self.scratch,"a"*32)
        self.assertEqual(snapshot["coverage"],"unknown")
        self.assertNotIn("secret-stage",json.dumps(snapshot))

    def test_supervisor_projection_discards_extra_payload_and_rejects_bad_types(self):
        import tick_progress
        with patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")):
            self.t.bounded("merge", "synthetic")
        original=self.read()
        original["progress"]["extra"]="secret-extra"
        original["progress"]["stages"][0]["stdout"]="secret-extra"
        (self.scratch/"tick.json").write_text(json.dumps(original))
        clean=tick_progress.snapshot(self.scratch,"a"*32)
        self.assertEqual(clean["coverage"],"observed")
        self.assertNotIn("secret-extra",json.dumps(clean))
        for key,value in (("duration_seconds",float("nan")),("exit",True),("outcome",[]),("step",[]),("outcome","failed")):
            doc=json.loads(json.dumps(original));doc["progress"]["stages"][0][key]=value
            (self.scratch/"tick.json").write_text(json.dumps(doc))
            self.assertEqual(tick_progress.snapshot(self.scratch,"a"*32)["coverage"],"unknown")

    def test_receipt_and_diagnostic_failure_do_not_replace_command_exit(self):
        import atomic
        def broken_logger(message):
            raise OSError("secret-log-detail")
        self.t.progress.logger=broken_logger
        with patch.object(atomic,"write_json",side_effect=OSError("secret-disk-detail")), \
             patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],19,"","")):
            self.assertEqual(self.t.bounded("merge","synthetic")[0],19)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO fixture")
    def test_fifo_receipt_is_unknown_without_blocking(self):
        os.mkfifo(self.scratch/"tick.json")
        probe="import sys;sys.path.insert(0,sys.argv[1]);import tick_progress;from pathlib import Path;print(tick_progress.snapshot(Path(sys.argv[2]),'a'*32)['coverage'])"
        try:
            result=subprocess.run([sys.executable,"-c",probe,str(ROOT),str(self.scratch)],
                                  capture_output=True,text=True,timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("optional stage receipt blocked on FIFO")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),"unknown")

    def test_symlink_receipt_is_unknown_and_does_not_copy_target(self):
        import tick_progress
        target=self.base/"foreign.json";target.write_text('{"secret":"synthetic-only"}')
        try:
            (self.scratch/"tick.json").symlink_to(target)
        except OSError:
            self.skipTest("symlink fixture unavailable")
        self.assertEqual(tick_progress.snapshot(self.scratch,"a"*32)["coverage"],"unknown")
        with patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],7,"","")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.t.bounded("merge","synthetic")[0],7)
        self.assertEqual(target.read_text(),'{"secret":"synthetic-only"}')
        self.assertTrue((self.scratch/"tick.json").is_symlink())

    def test_huge_duration_is_unknown_not_an_overflow(self):
        import tick_progress
        with patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")):
            self.t.bounded("merge","synthetic")
        doc=self.read();doc["progress"]["stages"][0]["duration_seconds"]=10**500
        (self.scratch/"tick.json").write_text(json.dumps(doc))
        self.assertEqual(tick_progress.snapshot(self.scratch,"a"*32)["coverage"],"unknown")

    def test_projection_exception_cannot_drop_supervisor_receipt(self):
        import tick_progress
        spec=importlib.util.spec_from_file_location("stage_supervisor",ROOT/"tools/tick_lease.py")
        supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)
        with patch.object(supervisor.paths,"SCRATCH",self.scratch), \
             patch.object(tick_progress,"snapshot",side_effect=OverflowError("secret-projection")):
            supervisor.record_run("2026-01-01T00:00:00Z","timeout",124,"ceiling",run_id="a"*32)
        report=json.loads((self.scratch/"tick-run.json").read_text())
        self.assertEqual((report["outcome"],report["exit"]),("timeout",124))
        self.assertEqual(report["stage_progress"]["coverage"],"unknown")
        self.assertNotIn("secret-projection",json.dumps(report))
        self.assertEqual(json.loads((self.scratch/"tick-runs.jsonl").read_text())["exit"],124)

    def test_final_report_includes_progress_but_legacy_fields_remain(self):
        with patch.object(self.tick.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")):
            self.t.bounded("merge", "synthetic")
        self.t.write_report()
        doc=self.read()
        self.assertEqual(doc["failed_steps"],[])
        self.assertEqual(doc["progress"]["stages"][-1]["outcome"],"ran")
        self.assertIn("finished_at",doc)

    def test_supervisor_stop_keeps_silent_stage_unknown_and_correlated(self):
        # Real process-group stop; fake command wait only, no operational collectors.
        child = """import sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import paths
from unittest.mock import patch
import importlib.util
spec=importlib.util.spec_from_file_location('synthetic_tick', Path(sys.argv[1])/'tools/tick.py')
tick=importlib.util.module_from_spec(spec);spec.loader.exec_module(tick)
with patch.object(tick.subprocess, 'run', side_effect=lambda *a, **kw: time.sleep(30)):
    tick.Tick(paths.SCRATCH).bounded('merge', 'synthetic')
"""
        driver = """import sys
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[1]+'/tools')
import tick_lease as tl
tl.configuration.validate_workspace=lambda **kwargs:None
tl.configuration.enabled=lambda *args:True
tl.ceiling_seconds=lambda:1
tl.SUPERVISOR_GRACE=0.1
raise SystemExit(tl.supervised([sys.executable,'-c',sys.argv[2],sys.argv[1]]))
"""
        env = {"PATH": os.defpath, "HOME": str(self.base), "USERPROFILE": str(self.base),
               "OBSERVATORY_HOME": str(self.base/'supervised'), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, '-c', driver, str(ROOT), child], env=env,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 124, result.stderr[-200:])
        scratch=self.base/'supervised/store/raw'
        report=json.loads((scratch/'tick-run.json').read_text())
        self.assertEqual(report['outcome'],'timeout')
        progress=report['stage_progress']; self.assertEqual(progress['coverage'],'partial')
        row=progress['stages'][-1]
        self.assertEqual((row['step'],row['outcome'],row['exit']),('merge','unknown',None))
        self.assertIsNone(row['ended_at']); self.assertIsNone(row['duration_seconds'])
        self.assertEqual(progress['run_id'],json.loads((scratch/'tick.json').read_text())['progress']['run_id'])
        history=json.loads((scratch/'tick-runs.jsonl').read_text().splitlines()[-1])
        self.assertEqual(history['stage_progress'],progress)

    def test_fixed_vocabulary_covers_current_tick_calls(self):
        import ast, tick_progress
        tree=ast.parse((ROOT/'tools/tick.py').read_text())
        names={n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call)
               and isinstance(n.func,ast.Name) and n.func.id in ('step','bounded')
               and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str)}
        self.assertTrue(names)
        self.assertFalse(names - tick_progress.STAGES)

    def test_write_failure_keeps_command_exit_and_fixed_diagnostic(self):
        import atomic
        out=io.StringIO()
        with patch.object(atomic,'write_json',side_effect=OSError('secret-disk-detail')), \
             patch.object(self.tick.subprocess,'run',return_value=subprocess.CompletedProcess([],17,'','')), \
             contextlib.redirect_stdout(out):
            self.assertEqual(self.t.bounded('merge','synthetic')[0],17)
        self.assertIn('stage receipt unavailable',out.getvalue())
        self.assertNotIn('secret-disk-detail',out.getvalue())

if __name__ == "__main__": unittest.main()
