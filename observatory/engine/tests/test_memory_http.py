#!/usr/bin/env python3
"""The loopback HTTP memory service, end to end (PB-137 N-016).

A real uvicorn server on 127.0.0.1 serves `mcp/http_service.py` over a temporary
workspace, and a real MCP client (the pinned SDK, 2026-07-28) calls it with a bearer
issued as access-bindings/1. Nothing reads the operator's workspace.

* a bound agent reads and writes memory in its project over HTTP;
* the door refuses before the body is read: no bearer, an unknown bearer, another
  audience, a revoked binding (401 with the typed envelope); a foreign Host (421) or
  Origin (403); an oversized body (413); too many requests (429);
* a local-only tool answers `local-only` over HTTP;
* two bindings calling at once each see only their own project;
* `X-Fabric-Projects` narrows over the wire;
* a restarted service serves the same store and remembers nothing else;
* `/mcp/` does not redirect anywhere.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "mcp"))
from test_memory_access import ALPHA, BETA, Workspace, tearDownModule  # noqa: E402,F401


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


MODERN = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
          "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}
LIST_BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {
    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
              "io.modelcontextprotocol/clientCapabilities": {},
              "io.modelcontextprotocol/clientInfo": {"name": "n016", "version": "0"}}}}).encode()


class Service:
    def __init__(self, port: int, rate: int = 1000) -> None:
        import uvicorn
        for name in ("server", "http_service"):
            sys.modules.pop(name, None)
        import http_service
        self.port = port
        app = http_service.build_app(port=port, rate=rate)
        self.srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                 log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.srv.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.srv.started:
                break
            time.sleep(0.05)
        if not self.srv.started:
            raise RuntimeError("the service did not start")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.srv.should_exit = True
        self.thread.join(timeout=15)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")
        self.ws.bind("agent:beta-bot", [BETA], bearer="tok-beta")
        self.svc = Service(free_port())

    def tearDown(self) -> None:
        self.svc.stop()
        self.ws.MA.serve_stdio()

    def raw(self, headers: dict, body: bytes = LIST_BODY, path: str = "/mcp") -> tuple[int, dict, dict]:
        import httpx2
        with httpx2.Client(timeout=30, follow_redirects=False) as c:
            r = c.post(f"http://127.0.0.1:{self.svc.port}{path}", headers=headers, content=body)
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            return r.status_code, dict(r.headers), payload

    def call(self, bearer: str, tool: str, args: dict, extra: dict | None = None) -> dict:
        return asyncio.run(self._call(bearer, tool, args, extra))

    async def _call(self, bearer: str, tool: str, args: dict, extra: dict | None = None) -> dict:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        headers = {"Authorization": f"Bearer {bearer}", **(extra or {})}
        async with httpx2.AsyncClient(headers=headers, timeout=60) as hc:
            async with Client(streamable_http_client(self.svc.url, http_client=hc),
                              mode="2026-07-28") as client:
                result = await client.call_tool(tool, args)
        if result.structured_content is not None:
            out = dict(result.structured_content)
            out = out.get("result", out) if set(out) == {"result"} else out
        else:
            out = json.loads(result.content[0].text)
        out["_isError"] = bool(result.is_error)
        return out


class ABoundAgentOverHttp(Base):
    def test_it_writes_and_reads_its_project(self) -> None:
        wf = self.call("tok-alpha", "observatory_checkpoint_write", {
            "owner": "agent:alpha-bot", "idempotencyKey": "key-http-0001", "stepId": "S1",
            "status": "in_progress", "body": {"goal": "export over http"}, "projectId": ALPHA})
        self.assertIn("leaseId", wf, wf)
        latest = self.call("tok-alpha", "observatory_checkpoint_latest",
                           {"workflowId": wf["workflowId"]})
        self.assertEqual(latest["checkpoint"]["body"]["goal"], "export over http")
        note = self.call("tok-alpha", "observatory_record", {
            "owner": "agent:alpha-bot", "statement": "the exporter runs nightly",
            "projectId": ALPHA})
        self.assertIn("memoryId", note, note)
        found = self.call("tok-alpha", "observatory_search",
                          {"query": "exporter nightly", "project_id": ALPHA})
        self.assertTrue(found["results"], found)
        self.assertTrue(found["receipt"]["receiptId"])
        rows = self.ws.journal()
        self.assertTrue(any(r["binding"] == "bnd_test0001" and r["allowed"] for r in rows))
        self.assertNotIn("tok-alpha", json.dumps(rows))

    def test_a_foreign_project_and_a_local_only_tool_are_refused(self) -> None:
        out = self.call("tok-alpha", "observatory_recall", {"projectId": BETA})
        self.assertEqual(out.get("code"), "project-not-bound", out)
        local = self.call("tok-alpha", "observatory_findings", {"limit": 1})
        self.assertTrue(local["_isError"])
        self.assertEqual(local.get("code"), "local-only", local)


class TheDoor(Base):
    def test_no_or_unknown_bearer_is_refused_before_the_body(self) -> None:
        status, headers, body = self.raw(MODERN)
        self.assertEqual((status, body.get("code")), (401, "no-credential"))
        self.assertIn("Bearer", headers.get("www-authenticate", ""))
        status, _, body = self.raw({**MODERN, "Authorization": "Bearer nobody"})
        self.assertEqual((status, body.get("code")), (401, "no-binding"))
        # Refused before the body is parsed: a body that is not JSON gets the same 401.
        status, _, body = self.raw({**MODERN, "Authorization": "Bearer nobody"}, body=b"{oops")
        self.assertEqual((status, body.get("code")), (401, "no-binding"))

    def test_another_audience_and_a_revoked_binding(self) -> None:
        self.ws.bind("agent:far", [ALPHA], bearer="tok-far", audience="observatory:elsewhere")
        status, _, body = self.raw({**MODERN, "Authorization": "Bearer tok-far"})
        self.assertEqual((status, body.get("code")), (401, "audience-mismatch"))
        status, _, _ = self.raw({**MODERN, "Authorization": "Bearer tok-alpha"})
        self.assertEqual(status, 200)
        self.ws.bindings[0]["revokedAt"] = "2026-01-01T00:00:00Z"
        self.ws.save()
        status, _, body = self.raw({**MODERN, "Authorization": "Bearer tok-alpha"})
        self.assertEqual((status, body.get("code")), (401, "binding-revoked"))

    def test_host_origin_and_size(self) -> None:
        auth = {"Authorization": "Bearer tok-alpha"}
        self.assertEqual(self.raw({**MODERN, **auth, "Host": "evil.example"})[0], 421)
        self.assertEqual(self.raw({**MODERN, **auth, "Origin": "http://evil.example"})[0], 403)
        with socket.create_connection(("127.0.0.1", self.svc.port), timeout=20) as sock:
            head = (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{self.svc.port}\r\n"
                    f"Authorization: Bearer tok-alpha\r\nContent-Type: application/json\r\n"
                    f"MCP-Protocol-Version: 2026-07-28\r\nContent-Length: {5 * 1024 * 1024}\r\n\r\n")
            sock.sendall(head.encode("ascii"))
            reply = sock.recv(256).decode("latin-1", "replace")
        self.assertTrue(reply.startswith("HTTP/1.1 413"), reply[:40])

    def test_a_trailing_slash_does_not_redirect_elsewhere(self) -> None:
        # Starlette answers 307 to the same path without the slash, built from the Host
        # header, which the door has already confined to loopback: it can only point here.
        status, headers, _ = self.raw({**MODERN, "Authorization": "Bearer tok-alpha"}, path="/mcp/")
        self.assertEqual((status, headers.get("location")), (307, self.svc.url))

    def test_a_binding_over_its_rate_is_told_when_to_retry(self) -> None:
        self.svc.stop()
        self.svc = Service(free_port(), rate=2)
        auth = {**MODERN, "Authorization": "Bearer tok-alpha"}
        codes = [self.raw(auth)[0] for _ in range(3)]
        self.assertEqual(codes[:2], [200, 200])
        status, headers, body = self.raw(auth)
        self.assertEqual((status, body.get("code")), (429, "rate-limited"))
        self.assertTrue(int(headers.get("retry-after", "0")) >= 1)
        self.assertEqual(self.raw({**MODERN, "Authorization": "Bearer tok-beta"})[0], 200,
                         "the limit is per binding")


class TheGateAlone(unittest.TestCase):
    """The door with a stub behind it: what it refuses never reaches the app. The SDK's
    own Host/Origin check is a second layer; this pins the first."""

    def setUp(self) -> None:
        self.ws = Workspace()
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")
        for name in ("server", "http_service"):
            sys.modules.pop(name, None)
        import http_service
        self.reached = []

        async def stub(scope, receive, send):
            self.reached.append(self.ws.MA.current())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        self.gate = http_service.BindingGate(stub, port=4242, rate=100)

    def tearDown(self) -> None:
        self.ws.MA.serve_stdio()

    def hit(self, headers: dict) -> int:
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "POST", "path": "/mcp",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
        asyncio.run(self.gate(scope, receive, send))
        return sent[0]["status"]

    def test_refusals_never_reach_the_app(self) -> None:
        here = {"Host": "127.0.0.1:4242", "Authorization": "Bearer tok-alpha"}
        self.assertEqual(self.hit({**here, "Host": "evil.example"}), 421)
        self.assertEqual(self.hit({**here, "Origin": "http://evil.example"}), 403)
        self.assertEqual(self.hit({"Host": "127.0.0.1:4242"}), 401)
        self.assertEqual(self.reached, [])
        self.assertEqual(self.hit({**here, "Origin": "http://127.0.0.1:4242"}), 200)
        self.assertEqual(self.reached[0]["channel"], "http",
                         "the app runs inside the request's own channel")
        self.assertNotIn("tok-alpha", json.dumps(self.reached[0]))


class UnauthenticatedFloods(Base):
    def test_a_peer_flooding_without_a_bearer_is_limited_before_the_journal(self) -> None:
        self.svc.stop()
        self.svc = Service(free_port(), rate=2)
        codes = [self.raw(MODERN)[0] for _ in range(6)]
        self.assertEqual(codes[:4], [401] * 4, codes)
        self.assertEqual(codes[4:], [429, 429], "past twice the binding rate, refused unread")
        self.assertLessEqual(len(self.ws.journal()), 4, "the journal stops growing")


class ConcurrencyAndRestart(Base):
    def test_two_bindings_at_once_each_see_their_own_project(self) -> None:
        # This process now serves HTTP and has no default channel (serve_http), so the
        # fixture writes as the local agent explicitly — which is itself the guarantee.
        self.assertEqual(self.ws.note(ALPHA, statement="x").get("code"), "unknown-channel")
        with self.ws.MA.channel({"channel": "stdio"}):
            self.ws.note(ALPHA, statement="alpha exporter fact")
            self.ws.note(BETA, statement="beta exporter fact")

        async def both():
            return await asyncio.gather(*[
                self._call(tok, "observatory_recall", {"projectId": p})
                for tok, p in [("tok-alpha", ALPHA), ("tok-beta", BETA)] * 3])
        answers = asyncio.run(both())
        for i, out in enumerate(answers):
            want = "alpha exporter fact" if i % 2 == 0 else "beta exporter fact"
            self.assertEqual([r["statement"] for r in out["records"]], [want], out)

    def test_fabric_projects_narrow_over_the_wire(self) -> None:
        self.ws.bind("service:fabric-hub", [ALPHA, BETA], bearer="tok-hub")
        out = self.call("tok-hub", "observatory_recall", {"projectId": BETA},
                        {"X-Fabric-Projects": ALPHA})
        self.assertEqual(out.get("code"), "project-not-bound", out)
        ok = self.call("tok-hub", "observatory_recall", {"projectId": ALPHA},
                       {"X-Fabric-Projects": ALPHA})
        self.assertNotIn("error", ok)

    def test_a_restarted_service_serves_the_same_store(self) -> None:
        self.call("tok-alpha", "observatory_record", {
            "owner": "agent:alpha-bot", "statement": "kept across a restart", "projectId": ALPHA})
        port = self.svc.port
        self.svc.stop()
        self.svc = Service(port)
        out = self.call("tok-alpha", "observatory_recall", {"projectId": ALPHA})
        self.assertIn("kept across a restart", [r["statement"] for r in out["records"]])


if __name__ == "__main__":
    unittest.main()
