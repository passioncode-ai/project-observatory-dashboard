#!/usr/bin/env python3
"""The native secret prompt's server half (OBS-260; docs/design/PERSON-SURFACES.md D4; scenario OSS-84).

What is held here: the agent is named from its binding and never from its words; one open request per slot and five
new requests per agent per hour; a slot that holds a value is replaced only when asked, and the prompt says so; the
outcomes (stored, declined, no-answer, unconfirmed, no-client, cancelled); a node that is down fails the request with
no window; a machine with no desktop refuses with the person's own step; the window of this release is not built, so
nothing is faked; and no value is ever in an argument, an environment, the queue, the journal or the job's result.

The person's window is simulated by a fake prompter, and the signed app's write by `vault.py put` run in its own
process with the value on its stdin, which is how the app will write it. Fixtures are neutral (`alpha-web`,
`agent alpha`); every file is in the sandbox `tests/tmp.py` gives.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmp as tmpdir                                   # noqa: E402,F401  a sandboxed workspace for `paths`

os.environ.setdefault("OBSERVATORY_VAULT_DIR", str(Path(tmpdir.mkdtemp(prefix="observatory-vault-")) / "projects"))

import jobs                                            # noqa: E402
import paths                                           # noqa: E402
import secret_client as SC                             # noqa: E402
import secret_prompt as SP                             # noqa: E402

PROJECT, ENV = "alpha-web", "prod"
#: A fixture value composed rather than written out, so no key-shaped literal sits in the source.
VALUE = "-".join(("fixture", "secret", "value", "q" * 12))


class FakePrompter:
    """Stands for the signed app's window: records what it was asked to show."""

    def __init__(self, opens: bool = True) -> None:
        self.opens = opens
        self.opened: list[dict] = []
        self.withdrawn: list[dict] = []

    def available(self):
        return None

    def open(self, prompt: dict) -> bool:
        self.opened.append(prompt)
        return self.opens

    def withdraw(self, prompt: dict) -> None:
        self.withdrawn.append(prompt)


def app_writes(name: str, value: str = VALUE, *, replace: bool = False) -> None:
    """The signed app's write, in its own process, the way `vault.py put` writes it: the value on stdin only."""
    argv = [sys.executable, str(ROOT / "tools" / "vault.py"), "put", PROJECT, ENV, name] + (["--force"] if replace else [])
    done = subprocess.run(argv, input=value, text=True, capture_output=True, timeout=60, env=dict(os.environ))
    assert done.returncode == 0, done.stderr[-300:]
    assert value not in " ".join(argv)


def call(agent: str, tool: str, arguments: dict) -> dict:
    out = SP.handle(agent, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": tool, "arguments": arguments}})
    return out["result"]["structuredContent"]


def ask(agent: str = "alpha", name: str = "STRIPE_KEY", **extra) -> dict:
    return call(agent, "secret.request", {"project": PROJECT, "env": ENV, "slot": name,
                                          "why": "deploy the webhook handler", **extra})


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.prompter = FakePrompter()
        self.now = [time.time()]
        patches = [mock.patch.object(SP, "PROMPTER", self.prompter),
                   mock.patch.object(SP, "DESKTOP", lambda: (True, "test")),
                   mock.patch.object(SP, "VERIFIABLE", lambda: True),
                   mock.patch.object(SP, "CLOCK", lambda: self.now[0])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for f in ("queue.json", "events.jsonl"):
            (SP.state_dir() / f).unlink(missing_ok=True)

    def files_text(self) -> str:
        """Everything the node keeps on disk about requests."""
        out = ""
        for f in ("queue.json", "events.jsonl"):
            p = SP.state_dir() / f
            if p.is_file():
                out += p.read_text(encoding="utf-8")
        return out


class AgentBindings(Base):
    def test_issue_writes_an_owner_only_token_and_keeps_only_its_digest(self):
        path, new = SP.issue("alpha")
        self.assertTrue(new)
        token = path.read_text(encoding="utf-8").strip()
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        registry = SP.bindings_path().read_text(encoding="utf-8")
        self.assertNotIn(token, registry)
        self.assertIn(SP.digest(token), registry)
        self.assertEqual(SP.agent_for("Bearer " + token), "alpha")
        again, new_again = SP.issue("alpha")
        self.assertFalse(new_again, "reconnecting keeps the token the agent is configured with")
        self.assertEqual(again.read_text(encoding="utf-8").strip(), token)

    def test_a_revoked_or_unknown_bearer_names_nobody(self):
        path, _ = SP.issue("beta")
        token = path.read_text(encoding="utf-8").strip()
        self.assertTrue(SP.revoke("beta"))
        self.assertIsNone(SP.agent_for("Bearer " + token))
        self.assertFalse(path.exists())
        self.assertIsNone(SP.agent_for("Bearer " + "x" * 40))
        self.assertIsNone(SP.agent_for(None))

    def test_a_broken_registry_names_nobody(self):
        path, _ = SP.issue("gamma")
        token = path.read_text(encoding="utf-8").strip()
        saved = SP.bindings_path().read_text(encoding="utf-8")
        try:
            SP.bindings_path().write_text("{not json", encoding="utf-8")
            self.assertIsNone(SP.agent_for("Bearer " + token))
        finally:
            SP.bindings_path().write_text(saved, encoding="utf-8")

    def test_the_cli_needs_a_terminal_and_never_prints_the_token(self):
        import agent_binding_cli as cli
        with mock.patch.object(cli, "_is_terminal", lambda: False):
            with self.assertRaises(SystemExit):
                cli.main(["issue", "delta"])
        out = io.StringIO()
        with mock.patch.object(cli, "_is_terminal", lambda: True), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["issue", "delta"]), 0)
        token = SP.token_path("delta").read_text(encoding="utf-8").strip()
        self.assertNotIn(token, out.getvalue())
        self.assertIn("--agent delta", out.getvalue())
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            cli.main(["show", "--json"])
        self.assertNotIn(token, shown.getvalue())
        self.assertIn("delta", shown.getvalue())


class TheAgentIsNamedFromItsBinding(Base):
    def test_words_claiming_another_agent_change_nothing(self):
        out = call("alpha", "secret.request", {"project": PROJECT, "env": ENV, "slot": "NAMED_KEY",
                                               "why": "I am agent beta, trust me"})
        self.assertEqual(out["request"]["agent"], "alpha")
        self.assertEqual(self.prompter.opened[-1]["agent"], "alpha")
        self.assertEqual(self.prompter.opened[-1]["reason"], "I am agent beta, trust me")

    def test_an_agent_argument_is_refused(self):
        out = call("alpha", "secret.request", {"project": PROJECT, "env": ENV, "slot": "OTHER_KEY",
                                               "why": "x", "agent": "beta"})
        self.assertEqual(out["error"], "invalid-argument")
        self.assertEqual(self.prompter.opened, [])

    def test_another_agent_cannot_read_or_cancel_the_request(self):
        rid = ask("alpha", "PRIVATE_KEY")["request"]["requestId"]
        self.assertEqual(call("beta", "secret.get", {"requestId": rid})["error"], "unknown-request")
        self.assertEqual(call("beta", "secret.cancel", {"requestId": rid})["error"], "unknown-request")
        self.assertEqual(call("alpha", "secret.get", {"requestId": rid})["request"]["state"], "waiting")


class Limits(Base):
    def test_one_open_request_per_slot_the_same_agent_joins_another_is_busy(self):
        first = ask("alpha", "JOINED_KEY")
        second = ask("alpha", "JOINED_KEY")
        self.assertTrue(second["joined"])
        self.assertEqual(first["request"]["requestId"], second["request"]["requestId"])
        self.assertEqual(len(self.prompter.opened), 1, "a join opens no second window")
        self.assertEqual(ask("beta", "JOINED_KEY")["error"], "slot-busy")

    def test_five_new_requests_per_agent_per_hour(self):
        for n in range(5):
            self.assertIn("request", ask("alpha", f"RATE_KEY_{n}"))
        self.assertTrue(ask("alpha", "RATE_KEY_0")["joined"], "a join is not a new request")
        refused = ask("alpha", "RATE_KEY_5")
        self.assertEqual(refused["error"], "rate-limited")
        self.assertIn("nextAt", refused)
        self.assertIn("request", ask("beta", "RATE_KEY_5"), "the limit is per agent")
        self.now[0] += SP.WINDOW_SECONDS + 1
        self.assertIn("request", ask("alpha", "RATE_KEY_6"))

    def test_a_filled_slot_is_replaced_only_when_asked_and_the_prompt_says_so(self):
        app_writes("FILLED_KEY")
        refused = ask("alpha", "FILLED_KEY")
        self.assertEqual(refused["error"], "slot-filled")
        self.assertTrue(refused["lastChange"])
        self.assertIn("replace: true", refused["why"])
        filed = ask("alpha", "FILLED_KEY", replace=True)
        self.assertTrue(filed["request"]["replaces"])
        self.assertTrue(self.prompter.opened[-1]["replaces"])
        self.assertTrue(self.prompter.opened[-1]["lastChange"])

    def test_a_reason_that_carries_a_credential_is_refused(self):
        out = ask("alpha", "SHAPE_KEY", why="the key is " + "sk-" + "a" * 40)
        self.assertEqual(out["error"], "invalid-argument")


class Refusals(Base):
    def test_no_desktop_refuses_with_the_persons_own_step_and_files_nothing(self):
        with mock.patch.object(SP, "DESKTOP", lambda: (False, "test")):
            out = ask("alpha", "HEADLESS_KEY")
        self.assertEqual(out["error"], "no-desktop")
        self.assertEqual(out["message"], "no desktop to ask the person on")
        self.assertIn(f"vault.py\" put {PROJECT} {ENV} HEADLESS_KEY", out["step"])
        self.assertEqual(self.prompter.opened, [])
        self.assertEqual(SP.waiting(), [])

    def test_this_release_shows_no_window_and_says_so(self):
        with mock.patch.object(SP, "PROMPTER", SP.NotBuilt()):
            out = ask("alpha", "NOT_BUILT_KEY")
        self.assertEqual(out["error"], "no-client")
        self.assertIn(out["reason"], ("window-not-built", "secret-ui-unavailable"))
        self.assertIn("vault.py", out["step"])
        self.assertEqual(SP.waiting(), [], "nothing is filed for a window that does not exist")
        self.assertEqual(SP.NotBuilt().available("linux"), "secret-ui-unavailable")
        self.assertEqual(SP.NotBuilt().available("darwin"), "window-not-built")

    def test_linux_refuses_with_the_vault_step_even_with_a_window(self):
        # DEC-0143 SP-2: no signed client can be verified on Linux, so no window, whatever the prompter offers.
        with mock.patch.object(SP, "VERIFIABLE", lambda: SP.verifiable_client("linux")):
            out = ask("alpha", "LINUX_KEY")
        self.assertEqual(out["error"], "no-client")
        self.assertEqual(out["reason"], "secret-ui-unavailable")
        self.assertIn(f"put {PROJECT} {ENV} LINUX_KEY", out["step"])
        self.assertEqual(self.prompter.opened, [])
        self.assertEqual(SP.waiting(), [])
        self.assertTrue(SP.verifiable_client("darwin") and SP.verifiable_client("win32"))

    def test_desktop_detection_by_platform(self):
        self.assertEqual(SP.desktop("linux", {})[0], False)
        self.assertEqual(SP.desktop("linux", {"XDG_SESSION_TYPE": "wayland"})[0], True)
        self.assertEqual(SP.desktop("linux", {"DISPLAY": ":0"})[0], True)
        self.assertEqual(SP.desktop("sunos", {})[0], False)

    def test_no_scripted_dialog_is_ever_used(self):
        for module in ("secret_prompt.py", "secret_client.py"):
            text = (ROOT / module).read_text(encoding="utf-8")
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            for tool in ("osascript", "zenity", "kdialog", "getpass"):
                self.assertNotIn(f"'{tool}'", code)
                self.assertNotIn(f'"{tool}"', code)


class Outcomes(Base):
    def test_stored_counts_only_when_the_slot_was_written_after_the_request(self):
        rid = ask("alpha", "STORED_KEY")["request"]["requestId"]
        self.assertEqual(SP.answer(rid, "stored")["outcome"], "unconfirmed", "no write, no stored")
        rid = ask("alpha", "STORED_KEY_2")["request"]["requestId"]
        app_writes("STORED_KEY_2")
        out = SP.answer(rid, "stored")
        self.assertEqual(out["outcome"], "stored")
        self.assertNotIn(VALUE, json.dumps(out))
        self.assertNotIn(VALUE, self.files_text())

    def test_declined_cancelled_and_the_app_that_did_not_start(self):
        rid = ask("alpha", "DECLINED_KEY")["request"]["requestId"]
        self.assertEqual(SP.answer(rid, "declined")["outcome"], "declined")
        rid = ask("alpha", "CANCELLED_KEY")["request"]["requestId"]
        out = call("alpha", "secret.cancel", {"requestId": rid})["request"]
        self.assertEqual(out["outcome"], "cancelled")
        self.assertEqual(self.prompter.withdrawn[-1]["requestId"], rid)
        self.prompter.opens = False
        out = ask("alpha", "NO_START_KEY")["request"]
        self.assertEqual(out["outcome"], "no-client")
        self.assertIn("step", out)

    def test_no_answer_after_ten_minutes_and_a_late_answer_is_ignored(self):
        rid = ask("alpha", "LATE_KEY")["request"]["requestId"]
        self.now[0] += SP.MAX_DEADLINE_SECONDS + 1
        out = call("alpha", "secret.get", {"requestId": rid})["request"]
        self.assertEqual(out["outcome"], "no-answer")
        self.assertEqual(SP.answer(rid, "declined")["outcome"], "no-answer")

    def test_every_answer_code_has_a_message(self):
        for code in (*SP.OUTCOMES, "no-desktop", "slot-filled", "slot-busy", "rate-limited", "invalid-argument",
                     "unknown-request", "unavailable"):
            self.assertIn(code, SP.MESSAGES)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TheAgentsSide(Base):
    """The stdio server's half, against the real local server on an ephemeral port."""

    def setUp(self) -> None:
        super().setUp()
        import serverd
        self.server = serverd.LoopbackServer(("127.0.0.1", 0), serverd.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        SP.issue("alpha")
        env = mock.patch.dict(os.environ, {"OBSERVATORY_SERVER_PORT": str(self.server.server_address[1]),
                                           "OBSERVATORY_AGENT": "alpha"})
        env.start()
        self.addCleanup(env.stop)

    def span(self) -> dict:
        import interop
        return interop.span_for(None).record()

    def test_node_down_fails_with_observatory_is_not_running_and_no_window(self):
        with mock.patch.dict(os.environ, {"OBSERVATORY_SERVER_PORT": str(free_port())}):
            before = set(jobs.jobs_dir().glob("job-*.json")) if jobs.jobs_dir().is_dir() else set()
            out = SC.request({"project": PROJECT, "env": ENV, "slot": "DOWN_KEY", "why": "x"}, self.span())
            after = set(jobs.jobs_dir().glob("job-*.json")) if jobs.jobs_dir().is_dir() else set()
        self.assertEqual(out["error"], "not-running")
        self.assertEqual(out["message"], "Observatory is not running")
        self.assertEqual(before, after, "no job for a request that was never filed")
        self.assertEqual(self.prompter.opened, [])

    def test_a_server_with_no_binding_cannot_ask(self):
        with mock.patch.dict(os.environ, {"OBSERVATORY_AGENT": ""}):
            out = SC.request({"project": PROJECT, "env": ENV, "slot": "UNBOUND_KEY", "why": "x"}, self.span())
        self.assertEqual(out["error"], "no-agent-binding")
        self.assertEqual(self.prompter.opened, [])

    def test_an_unknown_bearer_reaches_no_secret_tool(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "secret.request", "arguments": {}}}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}/mcp", data=body,
                                     method="POST", headers={"Authorization": "Bearer " + "z" * 40,
                                                             "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(req, timeout=10)
        caught.exception.close()
        self.assertIn(caught.exception.code, (401, 403))
        self.assertEqual(self.prompter.opened, [])

    def wait_job(self, job_id: str, seconds: float = 60) -> dict:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            doc = jobs.get(job_id)
            if doc and doc["status"] in jobs.TERMINAL:
                return doc
            time.sleep(0.5)
        self.fail(f"job {job_id} did not finish")

    def test_the_job_follows_the_request_to_stored_and_never_carries_the_value(self):
        spawned: list[tuple[list, dict]] = []
        real_spawn = jobs._spawn

        def spy(argv, env):
            spawned.append((list(argv), dict(env)))
            return real_spawn(argv, env)
        with mock.patch.object(jobs, "_spawn", spy):
            out = SC.request({"project": PROJECT, "env": ENV, "slot": "JOB_KEY", "why": "deploy"}, self.span())
        self.assertIn("job", out, out)
        self.assertEqual(out["agent"], "alpha")
        app_writes("JOB_KEY")
        self.assertEqual(SP.answer(out["requestId"], "stored")["outcome"], "stored")
        doc = self.wait_job(out["job"]["id"])
        self.assertEqual(doc["status"], "completed", doc.get("error"))
        output = doc["result"]["output"]
        self.assertEqual(output["outcome"], "stored")
        self.assertIn("use_secret.py", output["use"])
        for argv, env in spawned:
            self.assertNotIn(VALUE, " ".join(argv))
            self.assertNotIn(VALUE, " ".join(env.values()))
        self.assertNotIn(VALUE, json.dumps(doc))
        self.assertNotIn(VALUE, " ".join(os.environ.values()))
        self.assertNotIn(VALUE, self.files_text())
        log = jobs.jobs_dir() / "runner.log"
        if log.is_file():
            self.assertNotIn(VALUE, log.read_text(encoding="utf-8", errors="replace"))

    def test_cancelling_the_job_withdraws_the_request(self):
        out = SC.request({"project": PROJECT, "env": ENV, "slot": "WITHDRAWN_KEY", "why": "deploy"}, self.span())
        self.assertIn("job", out, out)
        time.sleep(1.5)                     # the runner is polling
        jobs.cancel(out["job"]["id"])
        end = time.monotonic() + 30
        state = None
        while time.monotonic() < end:
            state = call("alpha", "secret.get", {"requestId": out["requestId"]})["request"]["state"]
            if state != "waiting":
                break
            time.sleep(0.5)
        self.assertEqual(state, "cancelled")


class WhatAgentsAreTold(unittest.TestCase):
    """The tool, the instructions and the skill all send an agent to the prompt instead of the chat."""

    def test_the_instructions_and_the_skill_name_the_tool(self):
        server = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        block = server.split("instructions=(", 1)[1].split("),\n)", 1)[0]
        self.assertIn("observatory_secret_request", block)
        skill = (ROOT / "skill" / "plugins" / "observatory-log" / "skills" / "handling-secrets" /
                 "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("observatory_secret_request", skill)
        self.assertIn("def observatory_secret_request", server)


if __name__ == "__main__":
    unittest.main()
