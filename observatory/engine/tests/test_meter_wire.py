#!/usr/bin/env python3
"""The meter over the wire (OBS-55): `grant.check` on the memory-http door, end to end.

A real uvicorn server serves `mcp/http_service.py` over a temporary workspace whose `config/meter.json`
points at a throwaway estate key and a grant directory; a real MCP client presents an onward delegation token
in `Authorization: Bearer` (or the hub's `Fabric-Delegation`) and calls `grant.check`.

* an allowed check answers a receipt, a second one past the day's limit is denied at the window;
* the token opens `grant.check` alone: a memory tool and a local-only tool refuse its channel;
* a token for another audience is `principal-unproven`; a present invalid `Fabric-Delegation` header is
  the token, never a fallthrough to a binding;
* a malformed request is isError `invalid-input`; the token in an argument is refused as an unknown field;
* the stdio server answers `grant.check` with `principal-unproven`: stdio carries no token;
* the token never reaches the access journal.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT))
from test_memory_access import ALPHA, Workspace, tearDownModule  # noqa: E402,F401
from test_memory_http import Base, Service, free_port  # noqa: E402
from test_meter import Estate, ISSUER, claims, grant  # noqa: E402


def stamp(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class GrantCheckOverHttp(Base):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")
        self.estate = Estate()
        import paths
        grants = self.ws.home / "fabric-grants"
        grants.mkdir(mode=0o700)
        grants.chmod(0o700)
        doc = grant(startsAt=stamp(-1), expiresAt=stamp(30))
        (grants / f"{doc['meta']['id']}.json").write_text(json.dumps(doc))
        (grants / f"{doc['meta']['id']}.json").chmod(0o600)
        (self.ws.home / "jwks.json").write_text(json.dumps(self.estate.jwks))
        (paths.CONFIG / "meter.json").write_text(json.dumps(
            {"issuer": ISSUER, "jwks": str(self.ws.home / "jwks.json"), "grantsDir": str(grants)}))
        self.svc = Service(free_port())

    def token(self, **over) -> str:
        now = int(time.time())
        return self.estate.sign(claims(iat=now - 5, exp=now + 300, **over))

    def test_an_allowed_check_then_the_window(self) -> None:
        first = self.call(self.token(task="task-1"), "grant.check",
                          {"amountUsd": 1.4, "tokenJti": "tok-callee-000000001"})
        self.assertEqual((first["decision"], first["_isError"]), ("allowed", False), first)
        self.assertTrue(first["receipt"].startswith("rcpt_"))
        for i in (2, 3):
            self.assertEqual(self.call(self.token(task=f"task-{i}"), "grant.check",
                                       {"amountUsd": 1.4, "tokenJti": f"tok-callee-00000000{i}"})["decision"],
                             "allowed")
        denied = self.call(self.token(task="task-4"), "grant.check",
                           {"amountUsd": 1.4, "tokenJti": "tok-callee-000000004"})
        self.assertEqual((denied["decision"], denied["code"], denied["level"], denied["window"]),
                         ("denied", "limit-reached", "L1", "day"), denied)

    def test_the_token_opens_grant_check_alone(self) -> None:
        token = self.token()
        memory = self.call(token, "observatory_search", {"query": "anything", "project_id": ALPHA})
        self.assertTrue(memory["_isError"] or memory.get("error"), memory)
        local = self.call(token, "observatory_credentials", {"projectId": "alpha"})
        self.assertTrue(local["_isError"], local)
        journal = json.dumps(self.ws.journal())
        self.assertNotIn(token, journal)
        self.assertNotIn(token.split(".")[2], journal)

    def test_an_unproven_token_and_a_present_invalid_header(self) -> None:
        other = self.call(self.token(aud="copylot"), "grant.check",
                          {"amountUsd": 0.1, "tokenJti": "tok-callee-000000001"})
        self.assertEqual((other["decision"], other["code"]), ("denied", "principal-unproven"), other)
        # A binding bearer beside an invalid Fabric-Delegation: the header is the token, never ignored.
        forwarded = self.call("tok-alpha", "grant.check", {"amountUsd": 0.1, "tokenJti": "tok-callee-000000001"},
                              extra={"Fabric-Delegation": "not-a-token"})
        self.assertEqual(forwarded["code"], "principal-unproven", forwarded)
        hub = self.call("tok-alpha", "grant.check", {"amountUsd": 0.1, "tokenJti": "tok-callee-000000002"},
                        extra={"Fabric-Delegation": self.token()})
        self.assertEqual(hub["decision"], "allowed", hub)

    def test_a_binding_bearer_does_not_reach_the_meter(self) -> None:
        out = self.call("tok-alpha", "grant.check", {"amountUsd": 0.1, "tokenJti": "tok-callee-000000001"})
        self.assertEqual(out["code"], "principal-unproven", out)

    def test_malformed_requests(self) -> None:
        token = self.token()
        bad = self.call(token, "grant.check", {"amountUsd": -1, "tokenJti": "tok-callee-000000001"})
        self.assertTrue(bad["_isError"], bad)
        smuggled = self.call(token, "grant.check", {"amountUsd": 0.1, "tokenJti": "tok-callee-000000001",
                                                    "token": token})
        self.assertTrue(smuggled["_isError"], smuggled)
        self.assertNotIn(token, json.dumps(smuggled))


class GrantCheckOverStdio(unittest.TestCase):
    def test_stdio_carries_no_token(self) -> None:
        import asyncio
        ws = Workspace()
        ws.MA.serve_stdio()
        result = asyncio.run(ws.srv.server.call_tool("grant.check", {"amountUsd": 0.1,
                                                                     "tokenJti": "tok-callee-000000001"}))
        out = dict(result.structured_content)
        self.assertEqual((out["decision"], out["code"]), ("denied", "principal-unproven"), out)
        self.assertIn("stdio", out["reason"])

    def test_the_tool_is_listed_with_self_contained_schemas(self) -> None:
        import asyncio
        ws = Workspace()
        tools = {t.name: t for t in asyncio.run(ws.srv.server.list_tools())}
        tool = tools["grant.check"]
        self.assertNotIn("$ref\": \"common", json.dumps(tool.input_schema) + json.dumps(tool.output_schema))
        self.assertIn("call", tool.output_schema["$defs"]["denied"]["properties"]["window"]["enum"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
