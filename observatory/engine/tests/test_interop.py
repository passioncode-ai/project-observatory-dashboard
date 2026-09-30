#!/usr/bin/env python3
"""fabric-interop/0.1 over real stdio: capability tools, trace context, jobs.

Each case starts `mcp/server.py` as a child process in a temporary workspace and
talks to it as a host would. The rules checked are those of the Fabric
agent-registry contract C3, which the conformance probe of fabric-agent-adapter
does not know yet:

- C3.1 / FAC-SEM-017: every `mcp` capability is a tool of the same name whose
  schemas ARE the bundled published files; annotations follow the effect;
- C3.4: a request's `_meta.traceparent` comes back as a CHILD span — same trace,
  new span id — an invalid one starts a new trace, and a job hands its trace to the
  process it runs (`TRACEPARENT`);
- C3.2: a job answers a handle, `fabric.job.get` follows it to a result envelope,
  an id never issued is `unknown-job` and never a fresh job, a job survives the
  server that started it, a cancel stops the work and is not overwritten, a
  runner killed mid-work reads as `interrupted`, and a second start joins;
- nothing planted in an agent config reaches any answer.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import mcp_config_fixture as fx                                          # noqa: E402

import jsonschema                                                        # noqa: E402
from mcp import ClientSession, StdioServerParameters                     # noqa: E402
from mcp.client.stdio import stdio_client                               # noqa: E402
import mcp.client.session as _mcp_session                               # noqa: E402

_mcp_session.DISCOVER_TIMEOUT_SECONDS = max(_mcp_session.DISCOVER_TIMEOUT_SECONDS,
                                            float(os.environ.get("OBSERVATORY_MCP_STARTUP_BUDGET", "60")))

MANIFEST = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
KEY = "https://fabric.passioncode.ai/agent-contract/extensions/interop/0.1"
PARENT_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN = "00f067aa0ba902b7"
TRACEPARENT = f"00-{PARENT_TRACE}-{PARENT_SPAN}-01"
VALID = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


ENVELOPE_REQUIRED = ["id", "contractVersion", "outcome", "done", "proof", "scope", "notVerified",
                     "artifacts", "createdAt", "producer", "output", "usage"]
JOB_HANDLE = {"type": "object", "required": ["job"], "additionalProperties": False,
              "properties": {"job": {"type": "object", "required": ["id", "status"], "additionalProperties": False,
                                     "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 128,
                                                           "pattern": "^[A-Za-z0-9._:-]+$"},
                                                    "status": {"const": "working"}}}}}


def contract(name: str):
    """A validator for a vendored fabric-agent-contract schema."""
    import interop
    return interop.contract_validator(name)


def bundled(uri: str) -> dict:
    name = uri.split("/observatory/engine/fabric/", 1)[1]
    return json.loads((ROOT / "fabric" / name).read_text(encoding="utf-8"))


def as_dict(schema) -> dict:
    """A schema as the client received it, whatever model the SDK wrapped it in."""
    return schema if isinstance(schema, dict) else schema.model_dump(by_alias=True, exclude_none=True)


def payload(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


class Workspace(unittest.TestCase):
    """A temporary HOME, workspace, planted agent configs and a stand-in `claude`."""

    claude_sleep = "0"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "workspace"
        self.agents = self.base / "agents"
        fx.make_workspace(self.home, mcp_root=self.agents)
        fx.write_home(self.agents)
        (self.base / "user").mkdir()
        bin_dir = fx.stub_claude(self.base / "bin").parent
        self.trace_file = self.base / "claude-trace.txt"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "FABRIC_"))}
        env.update(HOME=str(self.base / "user"), OBSERVATORY_HOME=str(self.home),
                   PYTHONDONTWRITEBYTECODE="1", PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                   OBSERVATORY_TEST_TRACE_FILE=str(self.trace_file),
                   OBSERVATORY_TEST_CLAUDE_SLEEP=self.claude_sleep,
                   OBSERVATORY_DB=str(self.home / "store/observatory.db"))
        self.env = env

    def tearDown(self):
        # A job runner is a detached process; one left running would outlive the suite.
        for record in (self.home / "store/jobs").glob("job-*.json") if (self.home / "store/jobs").is_dir() else []:
            pid = json.loads(record.read_text()).get("pid")
            if isinstance(pid, int):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
        self.tmp.cleanup()

    def session(self):
        return _Session(StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp/server.py")],
                                              cwd=str(ROOT), env=self.env))

    def run_async(self, coro):
        return asyncio.run(coro)

    def job_record(self, job_id: str) -> dict:
        return json.loads((self.home / "store/jobs" / f"{job_id}.json").read_text(encoding="utf-8"))

    async def poll(self, s, job_id: str, until=("completed", "failed", "cancelled"), seconds=60):
        deadline = time.monotonic() + seconds
        while True:
            res = await s.call_tool("fabric.job.get", {"id": job_id})
            job = payload(res)["job"]
            if job["status"] in until or time.monotonic() > deadline:
                return res, job
            await asyncio.sleep(job.get("pollIntervalMs", 500) / 1000 / 4)


def live_members(pgid: int) -> list[str]:
    """Processes of a group that still run. A member that exited but was not yet
    reaped by its parent is a zombie: Linux still counts it in the group, so
    `killpg(pgid, 0)` succeeds there although nothing runs."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=10)
    rows = [line.split() for line in out.stdout.splitlines()]
    return [r[0] for r in rows if len(r) >= 3 and r[1] == str(pgid) and not r[2].startswith("Z")]


@contextlib.asynccontextmanager
async def _Session(params):
    """One server over stdio, discovered, as nested `async with` blocks.

    Nested, not entered by hand: an exception raised inside a hand-entered pair
    left the SDK's task groups waiting on each other, so a failing assertion hung
    the suite until the runner's timeout instead of failing."""
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.discover()
            yield s


class CapabilitiesAsTools(Workspace):
    def test_every_mcp_capability_is_a_tool_with_its_published_schemas(self):
        async def go():
            async with self.session() as s:
                return {t.name: t for t in (await s.list_tools()).tools}
        tools = self.run_async(go())
        for cap in MANIFEST["capabilities"]:
            if cap["profile"]["kind"] != "mcp":
                continue
            with self.subTest(capability=cap["name"]):
                self.assertIn(cap["name"], tools)
                tool = tools[cap["name"]]
                self.assertEqual(as_dict(tool.input_schema),
                                 bundled(cap["inputSchema"]), "FAC-SEM-017: input schema is the published one")
                job = (cap.get("extensions") or {}).get(KEY, {}).get("job") is True
                if job:
                    # DEC-0017 (OQ-0006): oneOf[result envelope, job handle], self-contained,
                    # exactly the contract's jobToolOutputSchema(output) — written out here.
                    self.assertEqual(as_dict(tool.output_schema), {"type": "object", "oneOf": [
                        {"type": "object", "required": ENVELOPE_REQUIRED,
                         "properties": {"output": bundled(cap["outputSchema"])}},
                        JOB_HANDLE]}, "FAC-SEM-017 for a job tool, object-rooted for MCP clients")
                else:
                    published = bundled(cap["outputSchema"])
                    if published.get("type") != "object":
                        published = {"type": "object", **published}
                    self.assertEqual(as_dict(tool.output_schema), published,
                                     "FAC-SEM-017: output schema is the published one, object-rooted")
                ann = tool.annotations
                if cap["effect"] == "none":
                    self.assertTrue(ann.read_only_hint)
                else:
                    self.assertFalse(ann.read_only_hint)
                    self.assertIs(ann.destructive_hint, cap["effect"] in {"delete", "merge", "deploy", "change-policy"})
        for name in ("fabric.job.get", "fabric.job.cancel", "observatory_status", "observatory_record"):
            self.assertIn(name, tools, "job tools beside the capabilities, and the old tools kept")
        # What an MCP client requires of every tool: an object schema at the root of
        # both schemas. Claude Code refuses the whole listing otherwise, so one tool
        # rooted in `oneOf` made all eighteen unreachable in 0.9.0.
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertEqual(as_dict(tool.input_schema).get("type"), "object")
                if tool.output_schema is not None:
                    self.assertEqual(as_dict(tool.output_schema).get("type"), "object")

    def test_the_vendored_contract_schemas_are_the_recorded_bytes(self):
        import hashlib
        folder = ROOT / "fabric/interop-schemas"
        recorded = dict(re.findall(r"^\| `([^`]+)` \| `([0-9a-f]{64})` \|$",
                                   (folder / "README.md").read_text(encoding="utf-8"), re.M))
        files = {p.name for p in folder.glob("*.schema.json")}
        self.assertEqual(set(recorded), files)
        for name in files:
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), recorded[name], name)

    def test_the_manifest_declares_the_interop_extension(self):
        self.assertIn(KEY, MANIFEST["provider"]["extensions"])
        jobs = [c["name"] for c in MANIFEST["capabilities"] if (c.get("extensions") or {}).get(KEY, {}).get("job")]
        self.assertEqual(jobs, ["machine.mcp.refresh"])

    def test_input_that_breaks_the_published_schema_is_an_error_answer(self):
        async def go():
            async with self.session() as s:
                return await s.call_tool("machine.mcp.inventory", {"unexpected": True}, meta={"traceparent": TRACEPARENT})
        res = self.run_async(go())
        self.assertTrue(res.is_error)
        self.assertEqual(json.loads(res.content[0].text)["error"], "invalid-input")
        self.assertNotIn("True", res.content[0].text, "the rule is named, the caller's value is not echoed")

    def test_the_inventory_answer_validates_and_holds_no_planted_value(self):
        async def go():
            async with self.session() as s:
                refresh = await s.call_tool("machine.mcp.refresh", {})
                _, job = await self.poll(s, payload(refresh)["job"]["id"])
                return job, await s.call_tool("machine.mcp.inventory", {})
        job, res = self.run_async(go())
        self.assertEqual(job["status"], "completed", job)
        self.assertFalse(res.is_error)
        data = payload(res)
        jsonschema.validate(data, bundled(next(c for c in MANIFEST["capabilities"]
                                               if c["name"] == "machine.mcp.inventory")["outputSchema"]))
        text = json.dumps(data) + json.dumps(job)
        self.assertEqual(fx.leaked(text), [])
        self.assertNotIn("planted", text)
        self.assertNotIn(str(self.base), text)
        self.assertTrue(any(s["name"] == "alpha-docs" and s["answers"] is True for s in data["servers"]))


class InstalledManifest(Workspace):
    def test_the_per_install_manifest_starts_a_server_for_its_own_workspace(self):
        """What a host does with the descriptor's manifest: run its connection as written."""
        env = {**self.env}
        builder = subprocess.run([sys.executable, "-c",
                                  "import sys, json; sys.path.insert(0, 'tools');"
                                  "from publish_contract import installed_manifest;"
                                  "print(json.dumps(installed_manifest('ws-test')))"],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(builder.returncode, 0, builder.stderr)
        doc = json.loads(builder.stdout)
        conn = doc["capabilities"][0]["profile"]["connection"]
        from urllib.parse import urlparse, unquote
        interpreter = unquote(urlparse(conn["executableRef"]).path)
        env.pop("OBSERVATORY_HOME")                 # only the manifest's own arguments name it
        env.pop("OBSERVATORY_DB")

        async def go():
            async with _Session(StdioServerParameters(command=interpreter, args=conn["args"], cwd="/", env=env)) as s:
                return await s.call_tool("machine.mcp.inventory", {})
        res = self.run_async(go())
        self.assertFalse(res.is_error, res.content[0].text[:200])
        self.assertIn("never scanned", json.dumps(payload(res)), "it read THIS workspace, which has no scan yet")
        self.assertEqual(doc["provider"]["extensions"][
            "https://fabric.passioncode.ai/agent-contract/extensions/service/0.1"]["descriptor"],
            "project-observatory.ws-test")


class Trace(Workspace):
    def call(self, meta):
        async def go():
            async with self.session() as s:
                return await s.call_tool("machine.mcp.inventory", {}, meta=meta)
        return self.run_async(go())

    def test_the_answer_is_a_child_span_of_the_callers(self):
        res = self.call({"traceparent": TRACEPARENT, "tracestate": "fabric=hop1"})
        m = VALID.match(res.meta["traceparent"])
        self.assertIsNotNone(m, res.meta)
        self.assertEqual(m.group(1), PARENT_TRACE, "same trace id")
        self.assertNotEqual(m.group(2), PARENT_SPAN, "a new span, not the caller's echoed back")
        self.assertEqual(res.meta.get("tracestate"), "fabric=hop1")

    def test_no_or_an_invalid_traceparent_starts_a_new_trace(self):
        for meta in ({}, {"traceparent": TRACEPARENT.upper()},
                     {"traceparent": f"00-{'0' * 32}-{PARENT_SPAN}-01"},
                     {"traceparent": f"ff-{PARENT_TRACE}-{PARENT_SPAN}-01"},
                     {"traceparent": f"00-{PARENT_TRACE}-{'0' * 16}-01"}):
            with self.subTest(meta=meta):
                m = VALID.match(self.call(meta).meta["traceparent"])
                self.assertIsNotNone(m)
                self.assertNotEqual(m.group(1), PARENT_TRACE)
                self.assertNotEqual(m.group(1), "0" * 32)


class Jobs(Workspace):
    def test_a_job_runs_to_a_result_envelope_on_the_callers_trace(self):
        async def go():
            async with self.session() as s:
                start = await s.call_tool("machine.mcp.refresh", {}, meta={"traceparent": TRACEPARENT})
                res, job = await self.poll(s, payload(start)["job"]["id"])
                return start, res, job
        start, res, job = self.run_async(go())
        handle = payload(start)["job"]
        self.assertEqual(handle["status"], "working")
        self.assertRegex(handle["id"], r"^job-[0-9a-f]{32}$")
        start_span = VALID.match(start.meta["traceparent"])
        self.assertEqual(start_span.group(1), PARENT_TRACE)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(VALID.match(res.meta["traceparent"]).group(1), PARENT_TRACE,
                         "every answer about the job carries the job's trace")
        errors = [e.message for e in contract("interop-job.schema.json").iter_errors({"job": job})]
        self.assertEqual(errors, [], "the answer is the contract's interop-job shape, envelope included")
        self.assertEqual([e.message for e in contract("interop-job-tool-output.schema.json")
                          .iter_errors(payload(start))], [], "the handle conforms to the job tool's union")
        self.assertEqual(job["result"]["trace"]["traceparent"], res.meta["traceparent"],
                         "FAC-SEM-022: the answer's traceparent is the envelope's")
        self.assertEqual(job["result"]["trace"]["traceparent"], start.meta["traceparent"],
                         "the envelope's trace is the span of the call that started the job")
        jsonschema.validate(job["result"]["output"], bundled(
            next(c for c in MANIFEST["capabilities"] if c["name"] == "machine.mcp.refresh")["outputSchema"]))
        self.assertEqual(job["result"]["usage"]["inputTokens"], 0)
        self.assertIsInstance(job["result"]["usage"]["wallMs"], int)
        self.assertNotIn("pollIntervalMs", job, "a finished job asks for no more polling")
        # C3.4 (b): the process the job called got the same trace on a child span.
        child_tp, _ = self.trace_file.read_text().split("\n", 1)
        child = VALID.match(child_tp)
        self.assertIsNotNone(child, child_tp)
        self.assertEqual(child.group(1), PARENT_TRACE)
        self.assertNotIn(child.group(2), (PARENT_SPAN, start_span.group(2)))

    def test_an_id_never_issued_is_unknown_and_never_a_fresh_job(self):
        async def go():
            async with self.session() as s:
                out = []
                for job_id, want in (("job-" + "a" * 32, "unknown-job"), ("probe-unknown-0a1b2c", "unknown-job"),
                                     ("job-short", "unknown-job"), ("../../etc/passwd", "invalid-input"),
                                     ("", "invalid-input")):
                    out.append((want, await s.call_tool("fabric.job.get", {"id": job_id}),
                                await s.call_tool("fabric.job.cancel", {"id": job_id})))
                return out
        for want, get, cancel in self.run_async(go()):
            for res in (get, cancel):
                self.assertTrue(res.is_error)
                self.assertEqual(json.loads(res.content[0].text)["error"], want)
        jobs_dir = self.home / "store/jobs"
        self.assertFalse(list(jobs_dir.glob("job-*.json")) if jobs_dir.is_dir() else [],
                         "asking about a job created none")

    def test_a_job_outlives_the_server_that_started_it(self):
        async def first():
            async with self.session() as s:
                start = await s.call_tool("machine.mcp.refresh", {})
                return payload(start)["job"]["id"]
        job_id = self.run_async(first())

        async def second():
            async with self.session() as s:
                return await self.poll(s, job_id)
        res, job = self.run_async(second())
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["status"], "completed", job)

        async def third():
            async with self.session() as s:
                return payload(await s.call_tool("fabric.job.get", {"id": job_id}))["job"]
        self.assertEqual(self.run_async(third())["result"], job["result"], "the same result, read again")


class SlowJobs(Workspace):
    claude_sleep = "30"

    def test_a_cancel_stops_the_work_and_stays_cancelled(self):
        async def go():
            async with self.session() as s:
                start = await s.call_tool("machine.mcp.refresh", {})
                job_id = payload(start)["job"]["id"]
                deadline = time.monotonic() + 20
                while self.job_record(job_id).get("pid") is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                while not self.trace_file.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)       # the stand-in claude is running now
                pid = self.job_record(job_id)["pid"]
                cancelled = payload(await s.call_tool("fabric.job.cancel", {"id": job_id}))["job"]
                gone = False
                for _ in range(100):
                    if not live_members(pid):
                        gone = True
                        break
                    await asyncio.sleep(0.1)
                await asyncio.sleep(1.5)
                after = payload(await s.call_tool("fabric.job.get", {"id": job_id}))["job"]
                return cancelled, gone, after
        cancelled, gone, after = self.run_async(go())
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(gone, "the runner's process group, stand-in claude included, is gone")
        self.assertEqual(after["status"], "cancelled", "the runner's exit did not overwrite the cancel")
        self.assertNotIn("result", after)

    def test_a_second_start_joins_the_running_job(self):
        async def go():
            async with self.session() as s:
                a = await s.call_tool("machine.mcp.refresh", {})
                b = await s.call_tool("machine.mcp.refresh", {})
                await s.call_tool("fabric.job.cancel", {"id": payload(a)["job"]["id"]})
                return payload(a)["job"]["id"], payload(b)["job"]["id"], b
        first, second, b = self.run_async(go())
        self.assertEqual(first, second)
        self.assertIn("already running", " ".join(c.text for c in b.content[1:]))

    def test_a_runner_killed_mid_work_reads_as_interrupted(self):
        async def go():
            async with self.session() as s:
                job_id = payload(await s.call_tool("machine.mcp.refresh", {}))["job"]["id"]
                deadline = time.monotonic() + 20
                while self.job_record(job_id).get("pid") is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                os.killpg(self.job_record(job_id)["pid"], signal.SIGKILL)
                await asyncio.sleep(0.5)
                return await self.poll(s, job_id, seconds=10)
        res, job = self.run_async(go())
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(job["error"]["code"], "interrupted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
