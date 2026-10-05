#!/usr/bin/env python3
"""memory/0.1 served under its own names (PB-137 N-025).

The contract's schema is vendored, not owned (`fabric/memory-schemas/`); the nine
`memory.*` capabilities run the same code as the `observatory_*` tools; inputs the
contract does not define are refused, not ignored; every answer fits the contract's
output or refusal shape; and the provider declaration is what the contract asks for.
Over the real MCP server module and a store in a temporary workspace (the fixture of
tests/test_memory_access.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "mcp"))
from test_memory_access import ALPHA, BETA, Workspace, tearDownModule  # noqa: E402,F401

PINNED_COMMIT = "aa2f0977bbbb32e9940e16b2654af84960abfb54"
NINE = ["memory.checkpoint.write", "memory.checkpoint.latest", "memory.handoff.create",
        "memory.handoff.accept", "memory.handoff.get", "memory.workflow.list",
        "memory.record", "memory.search", "memory.recall"]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        sys.modules.pop("memory_wire", None)
        import memory_wire
        self.W = memory_wire

    def call(self, name: str, args: dict):
        async def go():
            return await self.ws.srv.server.call_tool(name, args)
        return asyncio.run(go())

    def answer(self, name: str, args: dict) -> dict:
        result = self.call(name, args)
        self.assertFalse(result.is_error, result.content[0].text)
        return json.loads(result.content[0].text)


class TheSchemaIsTheContracts(Base):
    def test_the_vendored_copy_is_the_pinned_one(self) -> None:
        readme = (self.W.SCHEMA_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn(PINNED_COMMIT, readme)
        for f in (self.W.SCHEMA_FILE, self.W.COMMON_FILE):
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            self.assertIn(f"`{digest}`", readme, f"the README names {f.name}'s digest")

    def test_the_declaration_is_valid_and_complete(self) -> None:
        import jsonschema
        doc = json.loads(self.W.DECLARATION_FILE.read_text(encoding="utf-8"))
        self.assertEqual(doc, self.W.declaration(), "the shipped file is the code's mapping")
        s = self.W.schema()
        jsonschema.Draft202012Validator({"$ref": "#/$defs/declaration", "$defs": s["$defs"]}
                                        ).validate(doc)
        self.assertEqual(doc["capabilities"], NINE)
        served = {t.name for t in asyncio.run(self.ws.srv.server.list_tools())}
        for cap, tool in doc["compatibility"].items():
            self.assertIn(tool, served, f"{cap} names a tool this server serves")
            self.assertIn(cap, served)

    def test_listed_input_schemas_stand_alone(self) -> None:
        tools = {t.name: t for t in asyncio.run(self.ws.srv.server.list_tools())}
        for cap in NINE:
            text = json.dumps(tools[cap].input_schema)
            self.assertNotIn("$ref", text, f"{cap}: a client does not fetch $ref")
            self.assertFalse(tools[cap].input_schema.get("additionalProperties", True), cap)


class Inlining(Base):
    def test_local_and_common_refs_are_inlined(self) -> None:
        s = self.W.schema()
        defs = s["$defs"]
        local = self.W._inline({"$ref": "#/$defs/refusal"}, defs)
        self.assertEqual(local, defs["refusal"])
        common = self.W._inline({"x": {"$ref": "common.schema.json#/$defs/timestamp"}}, defs)
        self.assertNotIn("$ref", json.dumps(common))
        self.assertEqual(common["x"], self.W._common()["$defs"]["timestamp"])
        with self.assertRaises(ValueError):
            self.W._inline({"$ref": "https://elsewhere.example/x.json"}, defs)


class SameCodeSameAnswers(Base):
    def test_a_workflow_round_trip_under_the_contract_names(self) -> None:
        wf = self.answer("memory.checkpoint.write", {
            "owner": "agent:alpha-bot", "idempotencyKey": "key-wire-0001", "stepId": "S1",
            "status": "in_progress", "body": {"goal": "ship the wire"}, "projectId": ALPHA})
        self.assertIn("leaseId", wf)
        latest = self.answer("memory.checkpoint.latest", {"workflowId": wf["workflowId"]})
        self.assertEqual(latest["checkpoint"]["body"]["goal"], "ship the wire")
        listed = self.answer("memory.workflow.list", {"projectId": ALPHA})
        self.assertEqual([w["workflowId"] for w in listed["workflows"]], [wf["workflowId"]])
        h = self.answer("memory.handoff.create", {
            "owner": "agent:alpha-bot", "idempotencyKey": "key-wire-0002",
            "workflowId": wf["workflowId"], "to": {"provider": "anthropic"}, "reason": "limit",
            "leaseId": wf["leaseId"]})
        got = self.answer("memory.handoff.get", {"handoffId": h["handoffId"]})
        self.assertEqual(got["status"], "offered")
        took = self.answer("memory.handoff.accept", {
            "owner": "agent:beta-bot", "idempotencyKey": "key-wire-0003",
            "handoffId": h["handoffId"]})
        self.assertTrue(took["leaseId"])

    def test_record_search_recall_match_the_observatory_tools(self) -> None:
        note = self.answer("memory.record", {"owner": "agent:alpha-bot",
                                             "statement": "the wire exporter runs hourly",
                                             "projectId": ALPHA})
        self.assertEqual(note["state"], "proposed")
        found = self.answer("memory.search", {"query": "wire exporter hourly", "projectId": ALPHA})
        self.assertEqual([r["memoryId"] for r in found["results"]], [note["memoryId"]])
        mine = self.answer("memory.recall", {"projectId": ALPHA})
        theirs = self.ws.srv.observatory_recall(projectId=ALPHA)
        self.assertEqual([r["memory_id"] for r in mine["records"]],
                         [r["memory_id"] for r in theirs["records"]])
        self.assertIn(note["memoryId"], [r["memory_id"] for r in mine["records"]])


class RefusedNotIgnored(Base):
    def test_an_unknown_field_is_refused_by_path_never_by_value(self) -> None:
        secret = "zebra-orchard-" + "4417"
        out = self.answer("memory.recall", {"projectId": ALPHA, "sneaky": secret})
        self.assertEqual(out["error"], "invalid-input", out)
        self.assertNotIn(secret, json.dumps(out))
        out = self.answer("memory.search", {"query": "x", "project_id": ALPHA})
        self.assertEqual(out["error"], "invalid-input", "the tool's own spelling is not the contract's")

    def test_reserved_names_are_not_served(self) -> None:
        for name in ("memory.explain", "memory.forget", "memory.learning.propose"):
            result = self.call(name, {})
            self.assertTrue(result.is_error, name)
            self.assertIn("unknown-tool", result.content[0].text)

    def test_a_binding_refusal_fits_the_contracts_refusal_shape(self) -> None:
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")

        async def go():
            with self.ws.http("tok-alpha"):
                return await self.ws.srv.server.call_tool("memory.recall", {"projectId": BETA})
        result = asyncio.run(go())
        self.assertFalse(result.is_error, "a refusal is a typed answer the contract describes")
        out = json.loads(result.content[0].text)
        self.assertEqual(out["code"], "project-not-bound")


if __name__ == "__main__":
    unittest.main()
