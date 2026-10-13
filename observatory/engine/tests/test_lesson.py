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
from unittest.mock import patch

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


class ANewCommitAfterPromotion(Base):
    def test_the_promoted_record_is_archived_and_a_new_proposal_replaces_it(self) -> None:
        first = self.ws.srv.observatory_lesson(**EMITTED)
        from store import db as sdb
        from store import ledger as L
        conn = sdb.connect()
        try:
            with patch("store.lessons.has_support", return_value=True):
                L.corroborate(conn, first["memoryId"], by="agent:lesson-corroborator",
                              check={"how": "two projects"}, expected_revision=1)
        finally:
            conn.close()
        with patch("store.lessons.has_support", return_value=True):
            wording = self.ws.srv.observatory_lesson(**lesson(title="An absent input must fail "
                                                                     "its own check, always"))
        self.assertEqual((wording["outcome"], wording["state"]), ("revised", "observed"),
                         "a change of wording keeps the corroboration")
        out = self.ws.srv.observatory_lesson(**lesson(commit="0123456789abcdef"))
        jsonschema.Draft202012Validator(self.tool().output_schema).validate(out)
        self.assertEqual((out["outcome"], out["state"]), ("replaced", "proposed"), out)
        self.assertEqual(out["replaces"], f"{first['memoryId']}@4")
        self.assertNotEqual(out["memoryId"], first["memoryId"])
        states = {r["memory_id"]: r["state"] for r in self.rows()}
        self.assertEqual(states[first["memoryId"]], "archived")
        again = self.ws.srv.observatory_lesson(**lesson(commit="0123456789abcdef"))
        self.assertEqual((again["memoryId"], again["outcome"]), (out["memoryId"], "unchanged"))


class EvidenceRevisionSafety(Base):
    def test_a_lesson_cannot_inherit_an_ordinary_notes_promotion(self):
        first = self.ws.srv.observatory_lesson(**EMITTED)
        from store import db as sdb, ledger as L
        conn = sdb.connect()
        try:
            body = json.loads(L.current(conn, first["memoryId"])["body_json"])
            body["origin"]["projectId"] = OTHER_PROJECT
            body["origin"]["commit"] = "commit:deadbeefcafe"
            for state in ("observed",):
                note = L.append(conn, owner="agent:synthetic", statement="An ordinary note")
                if state == "observed":
                    L.corroborate(conn, note["memoryId"], by="agent:checker",
                                  check={"how": "A check of the ordinary note"}, expected_revision=1)
                prior = dict(L.current(conn, note["memoryId"]))
                with self.subTest(state=state), self.assertRaises(L.LedgerError):
                    L.append(conn, owner=prior["owner"], statement=prior["statement"],
                             kind="lesson", function="experiential", scope="global", state=state,
                             memory_id=prior["memory_id"], expected_revision=prior["revision"], body=body)
                self.assertEqual(dict(L.current(conn, note["memoryId"])), prior)
        finally:
            conn.close()

    def promoted(self):
        first = self.ws.srv.observatory_lesson(**EMITTED)
        from store import db as sdb, ledger as L
        conn = sdb.connect()
        try:
            # Seed formerly verified evidence; real Git promotion has separate coverage.
            with patch("store.lessons.has_support", return_value=True):
                L.corroborate(conn, first["memoryId"], by="agent:lesson-corroborator",
                              check={"how": "synthetic independent evidence"}, expected_revision=1)
        finally:
            conn.close()
        return first

    def test_generic_promotion_cannot_claim_unknown_project_evidence(self):
        first = self.ws.srv.observatory_lesson(**EMITTED)
        from store import db as sdb, ledger as L
        conn = sdb.connect()
        try:
            with self.assertRaises(L.LedgerError):
                L.corroborate(conn, first["memoryId"], by="agent:other",
                              check={"how": "claimed check"}, expected_revision=1)
        finally:
            conn.close()

    def test_unchanged_repeat_also_revalidates_and_is_then_idempotent(self):
        first = self.promoted()
        out = self.ws.srv.observatory_lesson(**EMITTED)
        again = self.ws.srv.observatory_lesson(**EMITTED)
        self.assertEqual((out["outcome"], out["state"]), ("replaced", "proposed"))
        self.assertEqual((again["outcome"], again["memoryId"]), ("unchanged", out["memoryId"]))

    def test_stale_scan_cannot_replace_a_newer_revision(self):
        first = self.promoted()
        from store import db as sdb, ledger as L, lessons as LS
        conn = sdb.connect()
        try:
            with patch.object(LS, "has_support", return_value=True):
                body = json.loads(L.current(conn, first["memoryId"])["body_json"])
                body["title"] = "A newer description is preserved"
                newer = LS.write(conn, L, owner=EMITTED["owner"], body=body, provenance=[])
            with self.assertRaises(L.RevisionConflict):
                LS.revalidate(conn, L, memory_id=first["memoryId"], expected_revision=2)
            self.assertEqual(L.current(conn, first["memoryId"])["revision"], newer["revision"])
        finally:
            conn.close()

    def test_expired_git_budget_is_unknown_and_does_not_spawn(self):
        import time
        from tools import corroborate as C
        origin = {"projectId": OTHER_PROJECT, "commit": "commit:deadbeefcafe"}
        with patch.object(C.safe_git, "run") as run:
            result, reason = C.resolve_origin(origin, {OTHER_PROJECT: ["unused"]},
                                              deadline=time.monotonic() - 1)
        self.assertIsNone(result)
        self.assertIn("budget", reason)
        run.assert_not_called()

    def test_operator_supported_lesson_is_not_replaced_by_agent(self):
        first = self.promoted()
        from store import db as sdb, ledger as L, lessons as LS
        conn = sdb.connect()
        try:
            L.transition(conn, first["memoryId"], owner="operator", to_state="supported",
                         expected_revision=2)
            prior = dict(L.current(conn, first["memoryId"]))
            body = json.loads(prior["body_json"])
            body["origin"]["commit"] = "commit:deadbeefcafe"
            with self.assertRaises(L.OwnerRefused):
                LS.write(conn, L, owner=EMITTED["owner"], body=body, provenance=[])
            self.assertEqual(dict(L.current(conn, first["memoryId"])), prior)
        finally:
            conn.close()

    def test_shared_revision_refuses_project_and_class_identity_mutation(self):
        first = self.promoted()
        from store import db as sdb, ledger as L
        conn = sdb.connect()
        try:
            prior = L.current(conn, first["memoryId"])
            for field in ("project", "class"):
                body = json.loads(prior["body_json"])
                if field == "project":
                    body["origin"]["projectId"] = OTHER_PROJECT
                else:
                    body["classKey"] = "another-class"
                with self.subTest(field=field), self.assertRaises(L.LedgerError):
                    L.append(conn, owner=prior["owner"], statement=prior["statement"],
                             kind="lesson", function="experiential", scope="global", state="observed",
                             memory_id=prior["memory_id"], expected_revision=prior["revision"], body=body)
        finally:
            conn.close()

    def test_failure_after_archive_rolls_back_ledger_outbox_and_index(self):
        first = self.promoted()
        from store import db as sdb, ledger as L, lessons as LS
        conn = sdb.connect()
        before = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("ledger", "outbox", "search_notes")}
        prior = dict(L.current(conn, first["memoryId"]))
        body = json.loads(prior["body_json"])
        body["origin"]["commit"] = "commit:deadbeefcafe"
        real, calls = L.insert_revision, []
        def fail_second(c, row):
            calls.append(row)
            if len(calls) == 2:
                raise RuntimeError("synthetic successor failure")
            return real(c, row)
        try:
            with patch.object(L, "insert_revision", side_effect=fail_second), self.assertRaises(RuntimeError):
                LS.write(conn, L, owner=prior["owner"], body=body, provenance=[])
        finally:
            conn.close()
        conn = sdb.connect()
        try:
            self.assertEqual(dict(L.current(conn, first["memoryId"])), prior)
            self.assertEqual({table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                              for table in before}, before)
        finally:
            conn.close()

    def test_same_commit_without_resolvable_support_becomes_a_proposal_on_revision(self):
        first = self.promoted()
        out = self.ws.srv.observatory_lesson(**lesson(title="An absent source needs its own explicit check"))
        self.assertEqual((out.get("outcome"), out.get("state")), ("replaced", "proposed"))
        self.assertNotEqual(out["memoryId"], first["memoryId"])


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

    def setUp(self):
        super().setUp()
        # Workspace reloads store modules; the migration must share their transaction context.
        import importlib
        import migrate_lessons
        importlib.reload(migrate_lessons)

    def bridge(self, commit: str) -> dict:
        a = EMITTED
        return self.ws.srv.observatory_record(
            owner=a["owner"], statement=f"lesson: {a['classKey']} — {a['title']}",
            why=f"{a['trigger']}; check: {a['check']}",
            evidence=[{"kind": "commit", "sha": commit},
                      {"kind": "lesson", "id": "L-889956db5299", "classKey": a["classKey"],
                       "project": a["origin"]["projectId"], "nouns": a["nouns"]}])

    def test_migration_serializes_identity_check_and_competing_lesson_write(self):
        import sqlite3
        from contextvars import Context
        import migrate_lessons as M
        from store import db as sdb, ledger as L, lessons as LS
        note = self.bridge("commit:" + EMITTED["origin"]["commit"][:12])
        conn, other = sdb.connect(), sdb.connect()
        other.execute("PRAGMA busy_timeout=0")
        body = LS.body_of({k: v for k, v in EMITTED.items() if k != "owner"})
        real, attempts = LS.existing, []
        def interleave(c, ck, pk):
            result = real(c, ck, pk)
            if c is conn and not attempts:
                attempts.append("started")
                try:
                    Context().run(LS.write, other, L, owner=EMITTED["owner"], body=body, provenance=[])
                except sqlite3.OperationalError:
                    attempts.append("blocked")
            return result
        try:
            with patch.object(LS, "existing", side_effect=interleave):
                out = M.migrate(conn, apply=True)
            self.assertEqual(attempts, ["started", "blocked"])
            self.assertEqual(len(out["moved"]), 1)
            again = LS.write(other, L, owner=EMITTED["owner"], body=body, provenance=[])
            self.assertEqual((again["outcome"], again["memoryId"]), ("unchanged", note["memoryId"]))
            self.assertEqual(len(LS.current_lessons(conn)), 1)
        finally:
            conn.close()
            other.close()

    def test_migration_rereads_candidate_revision_under_lock(self):
        import migrate_lessons as M
        from store import db as sdb, ledger as L
        note = self.bridge("commit:" + EMITTED["origin"]["commit"][:12])
        conn, other = sdb.connect(), sdb.connect()
        real = M.candidates
        def changed(c):
            rows = real(c)
            prior = L.current(other, note["memoryId"])
            L.append(other, owner=prior["owner"], memory_id=prior["memory_id"],
                     expected_revision=prior["revision"], statement="a newer note to preserve")
            return rows
        try:
            with patch.object(M, "candidates", side_effect=changed):
                out = M.migrate(conn, apply=True)
            self.assertEqual(out["moved"], [])
            self.assertIn("changed", out["skipped"][0]["why"])
            self.assertEqual(L.current(conn, note["memoryId"])["statement"], "a newer note to preserve")
        finally:
            conn.close()
            other.close()

    def test_generic_revision_refuses_erasure_between_read_and_insert(self):
        from store import db as sdb, ledger as L, lessons as LS
        first = self.ws.srv.observatory_lesson(**EMITTED)
        conn, other = sdb.connect(), sdb.connect()
        prior = L.current(conn, first["memoryId"])
        body = json.loads(prior["body_json"])
        class InterleavedConnection:
            def __enter__(self):
                conn.__enter__()
                return self
            def __exit__(self, *args):
                return conn.__exit__(*args)
            def execute(self, sql, args=()):
                cursor = conn.execute(sql, args)
                if sql.startswith("SELECT revision FROM tombstones"):
                    result = cursor.fetchone()
                    other.execute("INSERT INTO tombstones VALUES (?, 1, 'fixture', 'operator', '2026-01-01')",
                                  (first["memoryId"],))
                    other.commit()
                    class CapturedRead:
                        def fetchone(self):
                            return result
                    return CapturedRead()
                return cursor
        try:
            with self.assertRaises(L.LedgerError):
                L.append(InterleavedConnection(), owner=prior["owner"], kind="lesson",
                         function="experiential", scope="global", statement=prior["statement"],
                         body=body, memory_id=first["memoryId"], expected_revision=1)
            self.assertEqual(L.current(conn, first["memoryId"])["revision"], 1)
        finally:
            conn.close()
            other.close()

    def test_generic_new_and_converted_lessons_cannot_duplicate_active_identity(self):
        from store import db as sdb, ledger as L, lessons as LS
        self.ws.srv.observatory_lesson(**EMITTED)
        note = self.bridge("commit:" + EMITTED["origin"]["commit"][:12])
        conn = sdb.connect()
        body = LS.body_of({k: v for k, v in EMITTED.items() if k != "owner"})
        try:
            for identity in ({}, {"memory_id": note["memoryId"], "expected_revision": 1}):
                with self.subTest(identity=identity), self.assertRaises(L.LedgerError):
                    L.append(conn, owner=EMITTED["owner"], kind="lesson", function="experiential",
                             scope="global", statement=LS.statement_of(body), body=body, **identity)
            self.assertEqual(len(LS.current_lessons(conn)), 1)
            self.assertEqual(L.current(conn, note["memoryId"])["kind"], "note")
        finally:
            conn.close()

    def test_migration_preserves_promoted_and_operator_owned_notes(self):
        import migrate_lessons as M
        from store import db as sdb, ledger as L
        conn = sdb.connect()
        try:
            observed = self.bridge("commit:" + EMITTED["origin"]["commit"][:12])
            L.corroborate(conn, observed["memoryId"], by="agent:checker",
                          check={"how": "generic note check"}, expected_revision=1)
            owned = self.bridge("commit:" + EMITTED["origin"]["commit"][:12])
            p = L.current(conn, owned["memoryId"])
            L.append(conn, owner=L.OPERATOR, memory_id=p["memory_id"], expected_revision=1,
                     statement=p["statement"], why=p["why"], evidence=json.loads(p["evidence_json"]))
            before = [tuple(row) for row in conn.execute("SELECT * FROM ledger ORDER BY memory_id, revision")]
            for apply in (False, True):
                out = M.migrate(conn, apply=apply)
                self.assertEqual(out["moved"], [])
                self.assertEqual({r["memory_id"] for r in out["skipped"]},
                                 {observed["memoryId"], owned["memoryId"]})
            self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM ledger ORDER BY memory_id, revision")], before)
        finally:
            conn.close()

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
