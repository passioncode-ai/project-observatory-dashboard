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


class FakeBroker:
    def __init__(self, states=("ready",), refuse=None) -> None:
        self.states = list(states)
        self.refuse = refuse
        self.opened: list[tuple[str, str]] = []

    def open_for_prompt(self, target_id: str, key: str) -> dict:
        if self.refuse:
            raise RuntimeError(self.refuse)
        self.opened.append((target_id, key))
        return {"operationId": "op-1", "state": self.states[0]}

    def prompt_operation(self, operation_id: str) -> dict:
        if len(self.states) > 1:
            self.states.pop(0)
        return {"operationId": operation_id, "state": self.states[0]}


class FakeService:
    def __init__(self, targets: dict, broker: FakeBroker) -> None:
        self.targets = targets
        self.broker = broker

    def read(self) -> dict:
        return {"targets": self.targets}


class FakeHost:
    def __init__(self, service) -> None:
        self._service = service

    def service(self):
        return self._service


def app_bundle(engine: bool = True) -> str:
    """A stand-in for the installed app: a folder, with the bundled engine inside or without it."""
    root = Path(tmpdir.mkdtemp(prefix="observatory-app-")) / "Project Observatory.app"
    target = root / SP.BUNDLED_ENGINE
    target.parent.mkdir(parents=True)
    if engine:
        target.write_text("#!/bin/sh\n", encoding="utf-8")
    return str(root)


def observatory_target(desired: str = "on_demand", bundle: str = SP.OBSERVATORY_BUNDLE_ID,
                       path: str | None = None) -> dict:
    return {"targetId": "observatory.app", "kind": "app", "approved": True, "desiredState": desired,
            "path": path or app_bundle(),
            "identity": {"scheme": "designated-requirement/1", "bundleId": bundle, "teamId": "ABCDE12345",
                         "requirement": "x"}}


class TheBrokerOpensTheWindow(Base):
    """The prompter the local server installs (D4): the enrolled app, through the lifecycle broker, never directly."""

    def setUp(self) -> None:
        super().setUp()
        # The broker path is the Mac's; the tests run on every system as if on one, with the enrolled app intact.
        for p in (mock.patch.object(sys, "platform", "darwin"), mock.patch.object(SP, "APP_VERIFIED", lambda t: True)):
            p.start()
            self.addCleanup(p.stop)

    def broker_prompter(self, targets=None, broker=None) -> SP.BrokerPrompter:
        self.broker = broker or FakeBroker()
        service = FakeService({"observatory.app": observatory_target()} if targets is None else targets, self.broker)
        return SP.BrokerPrompter(lambda: FakeHost(service))

    def test_available_only_on_macos_with_the_app_enrolled(self):
        self.assertIsNone(self.broker_prompter().available("darwin"))
        self.assertEqual(self.broker_prompter().available("win32"), "broker-unsupported")
        self.assertEqual(self.broker_prompter().available("linux"), "secret-ui-unavailable")
        self.assertEqual(self.broker_prompter(targets={}).available("darwin"), "app-not-enrolled")
        other = {"other.app": observatory_target(bundle="com.example.alpha")}
        self.assertEqual(self.broker_prompter(targets=other).available("darwin"), "app-not-enrolled")
        held = {"observatory.app": observatory_target(desired="held_off")}
        self.assertEqual(self.broker_prompter(targets=held).available("darwin"), "app-held-off")
        bare = {"observatory.app": observatory_target(path=app_bundle(engine=False))}
        self.assertEqual(self.broker_prompter(targets=bare).available("darwin"), "engine-untrusted")
        self.assertEqual(SP.BrokerPrompter(lambda: None).available("darwin"), "app-not-enrolled")

    def test_a_request_asks_the_broker_for_the_app_with_its_id_and_nothing_else(self):
        with mock.patch.object(SP, "PROMPTER", self.broker_prompter()):
            out = ask("alpha", "BROKER_KEY")
        rid = out["request"]["requestId"]
        self.assertEqual(self.broker.opened, [("observatory.app", rid)])
        self.assertEqual(out["request"]["state"], "waiting")

    def test_a_start_that_fails_closes_the_request_no_client(self):
        prompter = self.broker_prompter(broker=FakeBroker(states=("running", "failed")))
        prompter.WATCH_SECONDS = 0.01
        with mock.patch.object(SP, "PROMPTER", prompter):
            rid = ask("alpha", "FAILED_START_KEY")["request"]["requestId"]
            end = time.monotonic() + 10
            while time.monotonic() < end and call("alpha", "secret.get", {"requestId": rid})["request"]["state"] == "waiting":
                time.sleep(0.02)
        out = call("alpha", "secret.get", {"requestId": rid})["request"]
        self.assertEqual((out["outcome"], out["reason"]), ("no-client", "app-not-started"))
        self.assertIn("vault.py", out["step"])

    def test_a_refused_start_answers_no_client_at_once(self):
        with mock.patch.object(SP, "PROMPTER", self.broker_prompter(broker=FakeBroker(refuse="held_off"))):
            out = ask("alpha", "REFUSED_START_KEY")["request"]
        self.assertEqual((out["outcome"], out["reason"]), ("no-client", "app-not-started"))

    def test_not_enrolled_files_nothing_and_names_the_enroll_step(self):
        with mock.patch.object(SP, "PROMPTER", self.broker_prompter(targets={})), \
                mock.patch.object(sys, "platform", "darwin"):
            out = ask("alpha", "UNENROLLED_KEY")
        self.assertEqual((out["error"], out["reason"]), ("no-client", "app-not-enrolled"))
        self.assertIn("lifecycle enroll", out["fix"])
        self.assertIn("vault.py", out["step"])
        self.assertEqual(SP.waiting(), [])

    def test_an_app_without_its_own_engine_files_nothing_and_says_so_at_once(self):
        # Today's release: the signed app carries no engine, so the window could take no key. The agent hears it now
        # with the person's step, not after ten minutes of a window nobody shows.
        bare = {"observatory.app": observatory_target(path=app_bundle(engine=False))}
        with mock.patch.object(SP, "PROMPTER", self.broker_prompter(targets=bare)):
            out = ask("alpha", "NO_ENGINE_KEY")
        self.assertEqual((out["error"], out["reason"]), ("no-client", "engine-untrusted"))
        self.assertIn("vault.py", out["step"])
        self.assertNotIn("fix", out)
        self.assertEqual(self.broker.opened, [], "the app is not even opened")
        self.assertEqual(SP.waiting(), [])

    def test_an_app_that_is_no_longer_the_signed_one_files_nothing(self):
        # A planted engine in a user-writable bundle breaks its signature: answered at once, never ten minutes of silence.
        with mock.patch.object(SP, "APP_VERIFIED", lambda t: False), \
                mock.patch.object(SP, "PROMPTER", self.broker_prompter()):
            out = ask("alpha", "PLANTED_KEY")
        self.assertEqual((out["error"], out["reason"]), ("no-client", "engine-untrusted"))
        self.assertEqual(self.broker.opened, [])
        self.assertEqual(SP.waiting(), [])

    def test_windows_has_no_window_in_this_release(self):
        with mock.patch.object(SP, "PROMPTER", self.broker_prompter()), mock.patch.object(sys, "platform", "win32"):
            out = ask("alpha", "WINDOWS_KEY")
        self.assertEqual((out["error"], out["reason"]), ("no-client", "broker-unsupported"))
        self.assertEqual(self.broker.opened, [])


class TheAppsDoor(Base):
    """`full assistant secret`: only the signed app's own child reads or answers, and the value stays in it."""

    def setUp(self) -> None:
        super().setUp()
        self.verified = [True]
        p = mock.patch.object(SP, "CLIENT_CHECK", lambda: self.verified[0])
        p.start()
        self.addCleanup(p.stop)

    def door(self, doc: dict) -> tuple[int, dict, str]:
        """The action as the app runs it: one JSON object on stdin, one on stdout."""
        from agent import assistant
        raw = json.dumps(doc).encode("utf-8")
        stdin = mock.Mock()
        stdin.buffer = io.BytesIO(raw)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(assistant, "require_workspace", lambda: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = assistant.main(["secret"])
        return code, json.loads(out.getvalue()), out.getvalue() + err.getvalue()

    def test_an_unverified_caller_reads_no_request_and_answers_none(self):
        rid = ask("alpha", "DOOR_KEY")["request"]["requestId"]
        self.verified[0] = False
        for doc in ({"op": "list"}, {"op": "store", "requestId": rid, "value": VALUE},
                    {"op": "decline", "requestId": rid},
                    {"op": "unavailable", "requestId": rid, "reason": "phrase-store-unavailable"}):
            code, answer, text = self.door(doc)
            self.assertEqual((code, answer), (1, {"error": "client-unverified"}), doc["op"])
            self.assertNotIn(VALUE, text)
            self.assertNotIn("deploy the webhook handler", text)
        self.assertFalse(SP.slot_state(PROJECT, ENV, "DOOR_KEY")["filled"])
        self.assertEqual(call("alpha", "secret.get", {"requestId": rid})["request"]["state"], "waiting")

    def test_an_empty_queue_is_answered_without_the_check(self):
        self.verified[0] = False
        self.assertEqual(self.door({"op": "list"})[1], {"requests": []})

    def test_the_app_reads_the_request_as_the_window_shows_it(self):
        rid = ask("alpha", "SHOWN_KEY", provider="stripe")["request"]["requestId"]
        shown = self.door({"op": "list"})[1]["requests"]
        self.assertEqual([r["requestId"] for r in shown], [rid])
        self.assertEqual((shown[0]["agent"], shown[0]["reason"], shown[0]["provider"], shown[0]["replaces"]),
                         ("alpha", "deploy the webhook handler", "stripe", False))
        self.assertEqual(shown[0]["slot"], {"env": ENV, "name": "SHOWN_KEY"})

    def test_store_writes_the_slot_in_this_process_and_the_value_goes_nowhere_else(self):
        rid = ask("alpha", "DOOR_STORED_KEY")["request"]["requestId"]
        code, answer, text = self.door({"op": "store", "requestId": rid, "value": VALUE + "\n"})
        self.assertEqual(code, 0, text)
        self.assertEqual(answer["request"]["outcome"], "stored")
        self.assertNotIn(VALUE, text)
        self.assertNotIn(VALUE, self.files_text())
        vault = SP._vault()
        self.assertEqual(vault._read_private(vault._slot(PROJECT, ENV, "DOOR_STORED_KEY")).strip(), VALUE)
        moves = vault.MOVES.read_text(encoding="utf-8")
        self.assertIn("DOOR_STORED_KEY", moves)
        self.assertNotIn(VALUE, moves)
        self.assertEqual(call("alpha", "secret.get", {"requestId": rid})["request"]["outcome"], "stored")
        # The same request cannot be answered twice.
        self.assertEqual(self.door({"op": "store", "requestId": rid, "value": VALUE})[1], {"error": "request-closed"})

    def test_a_replacement_rotates_and_keeps_the_old_value_archived(self):
        app_writes("DOOR_REPLACED_KEY")
        rid = ask("alpha", "DOOR_REPLACED_KEY", replace=True)["request"]["requestId"]
        newer = VALUE + "-newer"
        self.assertEqual(self.door({"op": "store", "requestId": rid, "value": newer})[1]["request"]["outcome"],
                         "stored")
        vault = SP._vault()
        slot = vault._slot(PROJECT, ENV, "DOOR_REPLACED_KEY")
        self.assertEqual(vault._read_private(slot).strip(), newer)
        self.assertTrue(list(slot.parent.glob("DOOR_REPLACED_KEY.retired-*")))

    def test_a_value_with_a_line_break_or_nothing_is_not_stored(self):
        rid = ask("alpha", "DOOR_BAD_KEY")["request"]["requestId"]
        for value, why in (("", "value-empty"), ("   ", "value-empty"), ("first\nsecond", "value-multiline")):
            code, answer, text = self.door({"op": "store", "requestId": rid, "value": value})
            self.assertEqual((code, answer), (1, {"error": why}), "the vault's reason reaches the window")
        self.assertFalse(SP.slot_state(PROJECT, ENV, "DOOR_BAD_KEY")["filled"])
        self.assertEqual(call("alpha", "secret.get", {"requestId": rid})["request"]["state"], "waiting")

    def test_an_unchanged_value_says_so(self):
        app_writes("DOOR_SAME_KEY")
        rid = ask("alpha", "DOOR_SAME_KEY", replace=True)["request"]["requestId"]
        self.assertEqual(self.door({"op": "store", "requestId": rid, "value": VALUE})[1], {"error": "value-unchanged"})
        self.assertEqual(call("alpha", "secret.get", {"requestId": rid})["request"]["state"], "waiting")

    def test_the_app_names_no_reason_but_its_phrase_store(self):
        rid = ask("alpha", "DOOR_ENGINE_KEY")["request"]["requestId"]
        out = self.door({"op": "unavailable", "requestId": rid, "reason": "engine-untrusted"})[1]
        self.assertEqual(out, {"error": "invalid-argument"}, "engine-untrusted is the node's answer, before filing")

    def test_decline_and_no_place_for_the_phrase(self):
        rid = ask("alpha", "DOOR_DECLINED_KEY")["request"]["requestId"]
        self.assertEqual(self.door({"op": "decline", "requestId": rid})[1]["request"]["outcome"], "declined")
        rid = ask("alpha", "DOOR_PHRASE_KEY")["request"]["requestId"]
        out = self.door({"op": "unavailable", "requestId": rid, "reason": "phrase-store-unavailable"})[1]["request"]
        self.assertEqual((out["outcome"], out["reason"]), ("no-client", "phrase-store-unavailable"))
        self.assertIn("vault.py", out["step"])
        self.assertEqual(self.door({"op": "unavailable", "requestId": "sreq_" + "a" * 24, "reason": "other"})[1],
                         {"error": "invalid-argument"})

    def test_the_door_takes_only_its_own_shapes(self):
        for doc in ({"op": "list", "extra": "x"}, {"op": "store", "requestId": "x"}, {"op": "drop"},
                    {"op": "decline", "requestId": 3}):
            self.assertEqual(self.door(doc)[1], {"error": "invalid-input"})


class TheSignedAppCheck(unittest.TestCase):
    """`signed_app_parent`: the door runs from inside the enrolled bundle, and its immediate parent is the signed app."""

    def setUp(self) -> None:
        self.bundle = app_bundle()
        self.inside = str(Path(self.bundle) / "Contents" / "Resources" / "engine" / "lib" / "secret_prompt.py")
        self.document = {"targets": {"observatory.app": observatory_target(path=self.bundle)}}
        self.asked: list[list[str]] = []

    def run_codesign(self, signed: str):
        def run(args, timeout=10):
            self.asked.append(list(args))
            return subprocess.CompletedProcess(args, 0 if args[-1] == signed else 3, "", "")
        return run

    def test_only_macos_and_only_an_enrolled_app(self):
        for platform in ("linux", "win32"):
            self.assertFalse(SP.signed_app_parent(platform, document=self.document, run=self.run_codesign("100"),
                                                  parent=100, source=self.inside))
        self.assertFalse(SP.signed_app_parent("darwin", document={"targets": {}}, run=self.run_codesign("100"),
                                              parent=100, source=self.inside))

    def test_the_parent_is_checked_against_the_enrolled_requirement(self):
        self.assertTrue(SP.signed_app_parent("darwin", document=self.document, run=self.run_codesign("100"),
                                             parent=100, source=self.inside))
        requirement = self.asked[0][self.asked[0].index("-R") + 1]
        self.assertIn('identifier "ai.passioncode.observatory"', requirement)
        self.assertIn('"ABCDE12345"', requirement)

    def test_a_grandparent_is_never_enough(self):
        # The app runs the engine its settings name for other actions; that engine's child finds the app two levels up.
        self.assertFalse(SP.signed_app_parent("darwin", document=self.document, run=self.run_codesign("300"),
                                              parent=200, source=self.inside))
        self.assertEqual([a[-1] for a in self.asked], ["200"], "only the immediate parent is asked")

    def test_a_door_outside_the_enrolled_bundle_is_refused_whoever_its_parent_is(self):
        outside = str(ROOT / "secret_prompt.py")
        self.assertFalse(SP.signed_app_parent("darwin", document=self.document, run=self.run_codesign("100"),
                                              parent=100, source=outside))
        self.assertEqual(self.asked, [], "no codesign run for an engine the app does not carry")


class ReviewFollowUps(Base):
    """The review of #378: what it found, held."""

    def test_the_bearer_is_compared_in_constant_time(self):
        SP.issue("epsilon")
        import hmac
        with mock.patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compared:
            SP.agent_for("Bearer " + "w" * 40)
        self.assertGreater(compared.call_count, 0)

    def test_a_reason_with_a_line_separator_or_a_bidi_control_is_refused(self):
        for bad in ("deploy\u2028the handler", "deploy\u2029x", "pay \u202eredli", "x\u2066y\u2069", "a\x07b"):
            out = ask("alpha", "BIDI_KEY", why=bad)
            self.assertEqual((out["error"], out["field"]), ("invalid-argument", "why"), repr(bad))
        self.assertEqual(SP.waiting(), [])

    def test_a_bad_project_is_reported_as_the_project_field(self):
        out = call("alpha", "secret.request", {"project": "../up", "env": ENV, "slot": "FIELD_KEY", "why": "x"})
        self.assertEqual((out["error"], out["field"]), ("invalid-argument", "project"))
        out = call("alpha", "secret.request", {"project": PROJECT, "env": ENV, "slot": "lower", "why": "x"})
        self.assertEqual((out["error"], out["field"]), ("invalid-argument", "slot"))

    def test_a_provider_that_carries_a_credential_is_refused(self):
        shaped = "ghp" + "_" + "b" * 36
        out = ask("alpha", "PROVIDER_KEY", provider=shaped.lower())
        self.assertEqual((out["error"], out["field"]), ("invalid-argument", "provider"))
        self.assertNotIn(shaped.lower(), self.files_text())

    def test_an_invalid_argument_is_journalled_by_its_field_only(self):
        call("alpha", "secret.request", {"project": PROJECT, "env": ENV, "slot": "J_KEY", "why": "x", "agent": "z"})
        lines = [json.loads(line) for line in (SP.state_dir() / "events.jsonl").read_text().splitlines()]
        self.assertEqual((lines[-1]["event"], lines[-1]["code"], lines[-1]["field"], lines[-1]["agent"]),
                         ("refused", "invalid-argument", "agent", "alpha"))

    def test_a_request_filed_before_a_failed_job_start_is_withdrawn(self):
        withdrawn: list[str] = []
        filed = {"request": {"requestId": "sreq_" + "c" * 24, "slot": "a/prod/B", "agent": "alpha",
                             "replaces": False, "deadline": "x"}, "joined": False}
        with mock.patch.object(SC, "call", lambda tool, args, **kw: filed if tool == "secret.request"
                               else withdrawn.append(args["requestId"]) or {}), \
                mock.patch.object(jobs, "start", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                SC.request({"project": PROJECT, "env": ENV, "slot": "B", "why": "x"}, {})
        self.assertEqual(withdrawn, ["sreq_" + "c" * 24])
        # A joined request belongs to the job already following it.
        withdrawn.clear()
        filed["joined"] = True
        with mock.patch.object(SC, "call", lambda tool, args, **kw: filed if tool == "secret.request"
                               else withdrawn.append(args["requestId"]) or {}), \
                mock.patch.object(jobs, "start", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                SC.request({"project": PROJECT, "env": ENV, "slot": "B", "why": "x"}, {})
        self.assertEqual(withdrawn, [])

    def test_the_runner_withdraws_its_request_when_the_binding_is_refused(self):
        withdrawn: list[str] = []

        def node(tool, args, **kw):
            if tool == "secret.cancel":
                withdrawn.append(args["requestId"])
                return {}
            raise SC.ClientRefused("binding-refused", "revoked")
        import interop
        job = {"id": "job-x", "trace": interop.span_for(None).record(),
               "arguments": {"requestId": "sreq_" + "d" * 24, "slot": "a/prod/B", "agent": "alpha"}}
        with mock.patch.object(SC, "call", node), mock.patch.object(jobs, "update", lambda *a, **k: None):
            with self.assertRaises(RuntimeError):
                SC.follow(job)
        self.assertEqual(withdrawn, ["sreq_" + "d" * 24])


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
