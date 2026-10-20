#!/usr/bin/env python3
"""The canary: does the request for a `why` reach the MODEL, or only the person?

The old ask was a Stop `systemMessage`. Claude Code records that as an attachment
of type `hook_system_message` and never renders it into the model's input — it is
a notice in the person's view. A hook's `additionalContext` is recorded as an
attachment of type `hook_additional_context` with a `rendered` text and a
`renderedRole`: the text the model actually receives. Both record shapes were read
from Claude Code transcripts on 2026-10-14; `tests/ask_why_transcript.json` holds
SYNTHETIC transcript lines with the same fields (no real session, project or person), one
record of each kind:

- `mem:0000000000000a01`, asked the old way at Stop;
- `mem:0000000000000b02`, asked the new way with the next prompt.

`why_asks.reached` is the classifier, and `why_asks.py reached <transcript> <id>`
is the same check an operator runs on a real session when
`observatory.why_unanswered` fires. The last case drives the real hooks end to end
and records their output the way the harness does, so a change that moves the ask
back to a person-only channel turns this suite red.
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402
import watched_repo                                                 # noqa: E402
import why_asks                                                     # noqa: E402

FIXTURE = ROOT / "tests/ask_why_transcript.json"
PLUGIN = ROOT / "skill/plugins/observatory-log"
OLD, NEW = "mem:0000000000000a01", "mem:0000000000000b02"


def lines() -> list[str]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["lines"]


class TheRecordedChannels(unittest.TestCase):
    def test_the_old_stop_notice_reached_only_the_person(self):
        self.assertEqual(why_asks.reached(lines(), OLD), {"model": 0, "person": 1})

    def test_the_prompt_context_reached_the_model(self):
        self.assertEqual(why_asks.reached(lines(), NEW), {"model": 1, "person": 0})

    def test_an_id_the_transcript_never_carried_reached_nobody(self):
        self.assertEqual(why_asks.reached(lines(), "mem:ffffffffffffffff"),
                         {"model": 0, "person": 0})

    def test_a_torn_line_is_skipped_not_fatal(self):
        self.assertEqual(why_asks.reached(["{torn " + NEW, *lines()], NEW)["model"], 1)

    def test_the_command_an_operator_runs_says_the_same(self):
        py = sys.executable
        # The command reads a session transcript as Claude Code writes it: JSON lines.
        transcript = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-reach-")) / "session.jsonl"
        transcript.write_text("\n".join(lines()) + "\n", encoding="utf-8")
        hit = subprocess.run([py, str(ROOT / "why_asks.py"), "reached", str(transcript), NEW],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(hit.returncode, 0, hit.stderr)
        self.assertEqual(json.loads(hit.stdout)["model"], 1)
        miss = subprocess.run([py, str(ROOT / "why_asks.py"), "reached", str(transcript), OLD],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(miss.returncode, 1, "a person-only ask did not reach the model")


class TheHooksUseTheModelChannel(unittest.TestCase):
    def test_a_real_ask_lands_in_the_model_channel_and_nowhere_else(self):
        work, repo, built = watched_repo.build()
        env = {**os.environ, **built,
               "OBSERVATORY_DB": str(work / "observatory.db"),
               "OBSERVATORY_SCRATCH": str(work / "scratch"),
               "OBSERVATORY_ROOT": str(ROOT), "OBSERVATORY_PYTHON": sys.executable,
               "CLAUDE_PLUGIN_ROOT": str(PLUGIN), "XDG_STATE_HOME": str(work / "state")}
        stop = subprocess.run(["bash", str(PLUGIN / "hooks/record-turn.sh")], input=json.dumps(
            {"session_id": "reach-1", "cwd": str(repo), "stop_hook_active": False}),
            text=True, capture_output=True, env=env, timeout=300)
        self.assertEqual(stop.returncode, 0, stop.stderr[-300:])
        self.assertEqual(stop.stdout.strip(), "", "nothing goes to the person's channel")
        mid = json.loads((work / "scratch/record-turn.json").read_text(encoding="utf-8"))["memoryId"]

        prompt = subprocess.run([sys.executable, str(PLUGIN / "hooks/prompt-why.py")],
                                input=json.dumps({"session_id": "reach-1",
                                                  "hook_event_name": "UserPromptSubmit"}),
                                text=True, capture_output=True, env=env, timeout=60)
        self.assertEqual(prompt.returncode, 0)
        out = json.loads(prompt.stdout)
        # The harness contract: only hookSpecificOutput, never a decision.
        self.assertEqual(sorted(out), ["hookSpecificOutput"])
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        context = hso["additionalContext"]
        self.assertIn(mid, context)

        # Recorded the way the fixture shows Claude Code records a prompt hook's
        # context: the fixture's own record, its text replaced by this hook's.
        template = json.loads(next(l for l in lines() if '"UserPromptSubmit"' in l))
        prefix = template["rendered"][0]["content"].split(template["attachment"]["content"][0])[0]
        template["attachment"]["content"] = [context]
        template["rendered"] = [{"content": prefix + context + "\n</system-reminder>"}]
        transcript = [json.dumps(template)]
        self.assertEqual(why_asks.reached(transcript, mid), {"model": 1, "person": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
