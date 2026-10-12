#!/usr/bin/env python3
"""A lesson is its own record kind, written by `observatory_lesson` and found by kind.

A retro entry's class is written so an agent in another project can use it before it
repeats the mistake. Until this tool the only doors were `observatory_record` (a note,
indistinguishable from thousands of turn records) and `observatory_learn` (a failure/fix
pair inside one project, which a retro entry is not).

* The published input schema is the emitter's: `lesson_emitter_fixture.json` is the shape
  task-pipeline's `lessons.py emit-observatory --tool observatory_lesson` prints (hand-written
  until that flag is re-added; then re-pinned to the real output), and its arguments must
  validate against `tools/list` and record as they are.
* One record per (classKey, project): a repeat revises or changes nothing, never mints.
* What a lesson may not carry — a path, a URL, an e-mail address, a `project:<slug>` id —
  is refused before any write; a credential shape is redacted, and the answer says where.
* Every answer matches the outputSchema the tool publishes.
* It is local-only: a binding over HTTP is refused before the handler runs.

Over the real MCP server module and a store in a temporary workspace (the fixture of
tests/test_memory_access.py).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_memory_access import ALPHA, Workspace, tearDownModule  # noqa: E402,F401

import jsonschema  # noqa: E402

FIXTURE = json.loads((ROOT / "tests" / "lesson_emitter_fixture.json").read_text(encoding="utf-8"))
EMITTED = FIXTURE["output"]["arguments"]
OTHER_PROJECT = "p-0123456789ab"


def lesson(**changes) -> dict:
    args = json.loads(json.dumps(EMITTED))
    for key, value in changes.items():
        if key in ("projectId", "commit"):
            args["origin"][key] = value
        else:
            args[key] = value
    return args


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()

    def tool(self, name: str = "observatory_lesson"):
        return {t.name: t for t in asyncio.run(self.ws.srv.server.list_tools())}[name]

    def call(self, args: dict, name: str = "observatory_lesson"):
        return asyncio.run(self.ws.srv.server.call_tool(name, args))

    def rows(self) -> list[dict]:
        from store import db as sdb
        conn = sdb.connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM ledger WHERE kind = 'lesson' ORDER BY memory_id, revision")]
        finally:
            conn.close()


class TheEmittersArguments(Base):
    def test_the_fixture_is_the_emitters_shape(self) -> None:
        self.assertEqual(FIXTURE["output"]["tool"], "observatory_lesson")
        self.assertIn("lessons.py", FIXTURE["emitter"]["script"])
        self.assertEqual(FIXTURE["emitter"]["provenance"], "hand-written")

    def test_the_published_input_schema_takes_exactly_the_emitted_arguments(self) -> None:
        schema = self.tool().input_schema
        self.assertEqual(set(schema["properties"]), set(EMITTED),
                         "observatory_lesson's arguments and lessons.py's emitted ones differ")
        self.assertEqual(set(schema["required"]), set(EMITTED))
        origin = schema["$defs"]["LessonOrigin"]
        self.assertEqual(set(origin["properties"]), set(EMITTED["origin"]))
        self.assertFalse(origin.get("additionalProperties", True))
        jsonschema.Draft202012Validator(schema).validate(EMITTED)

    def test_the_emitted_arguments_record_as_they_are(self) -> None:
        out = self.call(EMITTED)
        self.assertFalse(out.is_error, out.content)
        answer = out.structured_content
        jsonschema.Draft202012Validator(self.tool().output_schema).validate(answer)
        self.assertEqual(answer["outcome"], "created")
        self.assertEqual(answer["state"], "proposed")
        self.assertEqual(answer["origin"], {"projectId": EMITTED["origin"]["projectId"],
                                            "commit": "commit:" + EMITTED["origin"]["commit"][:12]})
        [row] = self.rows()
        self.assertEqual((row["kind"], row["function"], row["scope"], row["state"]),
                         ("lesson", "experiential", "global", "proposed"))
        self.assertEqual(row["confidence"], 0.5)
        self.assertIsNone(row["project_id"], "a lesson belongs to no one project")
        body = json.loads(row["body_json"])
        self.assertEqual(body["classKey"], EMITTED["classKey"])
        self.assertEqual(body["nouns"], EMITTED["nouns"])
        self.assertNotIn("project:", json.dumps(dict(row), default=str))
        self.assertTrue(row["statement"].startswith(f"lesson: {EMITTED['classKey']} — "))


class OnePerClassAndProject(Base):
    def test_a_repeat_changes_nothing_and_a_change_revises(self) -> None:
        first = self.ws.srv.observatory_lesson(**EMITTED)
        again = self.ws.srv.observatory_lesson(**EMITTED)
        self.assertEqual((again["memoryId"], again["revision"], again["outcome"]),
                         (first["memoryId"], 1, "unchanged"))
        changed = self.ws.srv.observatory_lesson(**lesson(title="An absent input must fail "
                                                                "its own check, every time"))
        self.assertEqual((changed["memoryId"], changed["revision"], changed["outcome"]),
                         (first["memoryId"], 2, "revised"))
        other = self.ws.srv.observatory_lesson(**lesson(projectId=OTHER_PROJECT))
        self.assertNotEqual(other["memoryId"], first["memoryId"],
                            "another project's copy of the class is its own record")
        self.assertEqual(len({r["memory_id"] for r in self.rows()}), 2)


class WhatALessonMayNotCarry(Base):
    def assert_refused(self, args: dict, words: str) -> None:
        before = len(self.rows())
        out = self.ws.srv.observatory_lesson(**args)
        self.assertEqual(out.get("code"), "invalid-lesson", out)
        self.assertIn(words, out["detail"])
        jsonschema.Draft202012Validator(self.tool().output_schema).validate(out)
        self.assertEqual(len(self.rows()), before, "nothing was written")

    def test_a_slug_project_id_is_refused(self) -> None:
        self.assert_refused(lesson(projectId=ALPHA), "pseudonymous")

    def test_a_path_a_url_and_an_address_are_refused(self) -> None:
        self.assert_refused(lesson(trigger="when /srv/build/checkout is dirty"),
                            "an absolute path")
        self.assert_refused(lesson(check="see https://example.com/runbook"), "a URL")
        self.assert_refused(lesson(title="Ask someone@example.com before the gate runs"),
                            "an e-mail address")
        self.assert_refused(lesson(trigger="two\nlines"), "line break")

    def test_a_class_key_the_redactor_would_change_is_refused(self) -> None:
        # A slug may be a long hex run, and the redactor replaces such runs: the stored
        # classKey would stop being a slug and stop matching the other project's copy.
        self.assert_refused(lesson(classKey="check-" + "0123456789abcdef" * 2 + "-x"),
                            "classKey")

    def test_the_refusal_names_both_routes(self) -> None:
        out = self.ws.srv.observatory_lesson(**lesson(projectId=ALPHA))
        self.assertIn("observatory_lesson", out["hint"])
        self.assertIn("observatory_record", out["hint"],
                      "an engine without this tool takes the record bridge")

    def test_the_wire_refuses_a_malformed_origin_as_malformed_input(self) -> None:
        out = self.call(lesson(projectId=ALPHA))
        self.assertTrue(out.is_error)
        self.assertEqual(self.rows(), [])

    def test_the_operator_cannot_be_claimed(self) -> None:
        out = self.ws.srv.observatory_lesson(**{**EMITTED, "owner": "operator"})
        self.assertEqual((out.get("error"), out.get("code")), ("owner refused", "owner-refused"))
        jsonschema.Draft202012Validator(self.tool().output_schema).validate(out)
        self.assertEqual(self.rows(), [])

    def test_a_credential_shape_is_redacted_and_the_answer_says_where(self) -> None:
        key = "sk-or-v1-" + "a1B2c3D4e5F6g7H8i9J0" * 3
        out = self.ws.srv.observatory_lesson(**lesson(check=f"rotate {key} first"))
        self.assertEqual(out["outcome"], "created", out)
        self.assertIn("check", out["redacted"]["fields"])
        self.assertGreaterEqual(out["redacted"]["shapes"], 1)
        stored = json.dumps(self.rows())
        self.assertNotIn(key, stored)
        self.assertIn("[redacted]", stored)
        self.assertIn("commit:" + EMITTED["origin"]["commit"][:12], stored,
                      "the commit form survives the redactor")


class FoundByKind(Base):
    def test_search_and_recall_find_it_across_projects_by_kind(self) -> None:
        self.ws.srv.observatory_lesson(**EMITTED)
        self.ws.note(ALPHA, statement="an absent input file passed the gate silently")
        found = self.ws.srv.observatory_search(query="absent input gate", kinds=["lesson"])
        self.assertEqual([h["kind"] for h in found["results"]], ["lesson"], found)
        hit = found["results"][0]
        self.assertEqual(hit["lesson"]["classKey"], EMITTED["classKey"])
        self.assertEqual(hit["lesson"]["origin"]["projectId"], EMITTED["origin"]["projectId"])
        everything = self.ws.srv.observatory_search(query="absent input gate")
        self.assertTrue(everything["results"])
        self.assertTrue(all("kind" in h for h in everything["results"]),
                        "every hit names its kind")
        recalled = self.ws.srv.observatory_recall(kinds=["lesson"])
        self.assertEqual((recalled["total"], recalled["records"][0]["kind"]), (1, "lesson"))


class LocalOnly(Base):
    def test_a_binding_is_refused_before_the_handler(self) -> None:
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")

        async def over_http():
            with self.ws.http("tok-alpha"):
                return await self.ws.srv.server.call_tool("observatory_lesson", EMITTED)
        out = asyncio.run(over_http())
        self.assertTrue(out.is_error)
        self.assertEqual(json.loads(out.content[0].text)["code"], "local-only")
        self.assertEqual(self.rows(), [])


class TheBridgeNotesMigrate(Base):
    """Lessons written before this tool, as `observatory_record` notes (the emitter's
    default `--tool observatory_record`), become lesson records: same id, a new revision."""

    def bridge(self, commit: str) -> dict:
        a = EMITTED
        return self.ws.srv.observatory_record(
            owner=a["owner"], statement=f"lesson: {a['classKey']} — {a['title']}",
            why=f"{a['trigger']}; check: {a['check']}",
            evidence=[{"kind": "commit", "sha": commit},
                      {"kind": "lesson", "id": "L-889956db5299", "classKey": a["classKey"],
                       "project": a["origin"]["projectId"], "nouns": a["nouns"]}])

    def test_a_note_becomes_a_lesson_and_a_dry_run_writes_nothing(self) -> None:
        import importlib
        import migrate_lessons as M
        importlib.reload(M)
        from store import db as sdb
        note = self.bridge("commit:" + EMITTED["origin"]["commit"][:12])
        lost = self.bridge(EMITTED["origin"]["commit"])     # a full id: the redactor took it
        conn = sdb.connect()
        try:
            dry = M.migrate(conn, apply=False)
            self.assertEqual(self.rows(), [], "a dry run writes nothing")
            done = M.migrate(conn, apply=True)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual([m["memory_id"] for m in dry["moved"]], [note["memoryId"]])
        self.assertEqual([m["memory_id"] for m in done["moved"]], [note["memoryId"]])
        self.assertEqual([s["memory_id"] for s in done["skipped"]], [lost["memoryId"]])
        self.assertIn("origin.commit", done["skipped"][0]["why"])
        [row] = [r for r in self.rows() if r["memory_id"] == note["memoryId"]]
        self.assertEqual((row["revision"], row["state"], row["scope"]), (2, "proposed", "global"))
        self.assertIn("migrated-from", row["provenance_json"])
        again = self.ws.srv.observatory_lesson(**EMITTED)
        self.assertEqual((again["memoryId"], again["outcome"]), (note["memoryId"], "unchanged"),
                         "the migrated record is this project's record of the class")


if __name__ == "__main__":
    unittest.main()
