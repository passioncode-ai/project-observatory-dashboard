#!/usr/bin/env python3
"""access-bindings/1 enforced at every business entry point (PB-137 N-008).

`access_binding.py` decides (its own cases are tests/test_access_binding.py). This suite
proves the engine ASKS, the same way at every door, over a real MCP server module and a
real store in a temporary workspace:

* the local stdio agent is served as before, even when the registry is broken;
* an HTTP channel reaches only the memory tools, only for its bound projects, scopes,
  classes and effect, and only as its own principal — and every refusal happens before
  a side effect;
* a target decides the project (a workflow of another project is refused, and so is one
  that does not exist), a session binding touches only its workflows, and Fabric's
  `X-Fabric-Projects` narrows and never widens;
* revocation, expiry, a wrong audience and a rolled-back registry take effect at once;
* an idempotency key is kept per binding, so another caller never receives a lease token;
* every decision about a binding is journalled without a bearer or its digest;
* the operator's CLI issues a bearer only into a new owner-only file and never prints it.

Nothing here opens a socket: an HTTP request is represented by the channel the transport
will set (`memory_access.channel`), which is the seam N-016 plugs into.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "tools"))

_MADE: list[pathlib.Path] = []
_CACHED = ("paths", "configuration", "survey", "store", "memory_access", "access_binding",
           "embedding_policy", "proposals", "textkeys", "providers", "capability_tools",
           "srv_access", "access_binding_cli", "service_identity")

ALPHA, BETA, GAMMA = "project:alpha-web", "project:beta-api", "project:gamma-ops"
EXECUTOR = {"provider": "anthropic", "model": "claude-opus-5-5", "accountRef": "acct-a"}


def tearDownModule() -> None:
    for path in _MADE:
        shutil.rmtree(path, ignore_errors=True)


def _stamp(delta_days: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Workspace:
    """A fresh workspace with its own store and config; the server module loaded on it."""

    def __init__(self) -> None:
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="observatory-access-")).resolve()
        _MADE.append(self.home)
        os.environ["OBSERVATORY_HOME"] = str(self.home)
        os.environ["OBSERVATORY_DB"] = str(self.home / "store" / "observatory.db")
        os.environ["OBSERVATORY_STATE"] = str(self.home / "store")
        (self.home / "store").mkdir(parents=True, exist_ok=True)
        for name in [k for k in list(sys.modules)
                     if k in _CACHED or k.startswith("store.")]:
            sys.modules.pop(name, None)
        import paths
        paths.CONFIG.mkdir(parents=True, exist_ok=True)
        (paths.CONFIG / "models.json").write_text(
            (ROOT / "defaults/models.json").read_text(encoding="utf-8"), encoding="utf-8")
        paths.REGISTRY.mkdir(parents=True, exist_ok=True)
        for name, key in (("projects.json", "projects"), ("repositories.json", "repositories"),
                          ("memberships.json", "memberships")):
            (paths.REGISTRY / name).write_text(json.dumps({key: []}), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("srv_access", ROOT / "mcp/server.py")
        self.srv = importlib.util.module_from_spec(spec)
        sys.modules["srv_access"] = self.srv
        spec.loader.exec_module(self.srv)
        import memory_access
        import access_binding
        self.MA, self.AB = memory_access, access_binding
        self.revision = 0
        self.bindings: list[dict] = []

    # ── the registry ────────────────────────────────────────────────────────
    def bind(self, principal: str, projects: list[str], *, bearer: str,
             scopes=("memory.read", "memory.search", "memory.record", "memory.checkpoint",
                     "memory.handoff"),
             class_ceiling: str = "project-internal", effect: str = "propose",
             workflows: list[str] | None = None, expires_days: float = 30,
             audience: str | None = None) -> dict:
        n = len(self.bindings) + 1
        b = {"bindingId": f"bnd_test{n:04d}", "principal": principal, "channel": "http",
             "audience": audience or self.MA.own_audience(),
             "credentialRef": {"kind": "bearer-sha256", "digest": self.AB.bearer_digest(bearer)},
             "projects": projects, "workflows": workflows, "scopes": list(scopes),
             "classCeiling": class_ceiling, "effectCeiling": effect,
             "issuedAt": _stamp(-1), "expiresAt": _stamp(expires_days), "revokedAt": None,
             "issuedBy": "operator", "via": "terminal"}
        self.bindings.append(b)
        self.save()
        return b

    def save(self, revision: int | None = None) -> None:
        self.revision = revision if revision is not None else self.revision + 1
        self.MA.registry_path().write_text(json.dumps(
            {"schema": self.AB.SCHEMA, "revision": self.revision, "bindings": self.bindings}),
            encoding="utf-8")

    def http(self, bearer: str | None, fabric_projects: str | None = None,
             audience: str | None = None):
        return self.MA.channel(self.MA.http_channel(audience=audience, bearer=bearer,
                                                    fabric_projects=fabric_projects))

    def journal(self) -> list[dict]:
        path = self.home / "store" / "logs" / "access.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def count(self, table: str) -> int:
        from store import db as sdb
        conn = sdb.connect()
        try:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    # ── tool shorthands ─────────────────────────────────────────────────────
    def start(self, project: str, owner: str = "agent:alpha-bot", key: str = "key-start-0001"):
        return self.srv.observatory_checkpoint_write(
            owner=owner, idempotencyKey=key, stepId="S1", status="in_progress",
            body={"goal": f"work on {project}"}, projectId=project, executor=EXECUTOR)

    def note(self, project: str, owner: str = "agent:alpha-bot", statement: str = "x",
             classification: str | None = None):
        out = self.srv.observatory_record(owner=owner, statement=statement, projectId=project)
        if classification and "memoryId" in out:
            from store import db as sdb
            conn = sdb.connect()
            conn.execute("UPDATE ledger SET classification = ? WHERE memory_id = ?",
                         (classification, out["memoryId"]))
            conn.commit()
            conn.close()
        return out


def refused(out, code: str) -> bool:
    return isinstance(out, dict) and out.get("error") == "binding refused" and out.get("code") == code


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()


class TheLocalAgent(Base):
    def test_stdio_is_served_as_before(self) -> None:
        wf = self.ws.start(ALPHA)
        self.assertIn("leaseId", wf, wf)
        self.assertIn("workflows", self.ws.srv.observatory_workflow_list())
        self.assertNotIn("error", self.ws.srv.observatory_recall())
        self.assertEqual(self.ws.journal(), [], "the local agent's allowed calls are not journalled")

    def test_a_broken_registry_still_serves_stdio_and_refuses_http(self) -> None:
        self.ws.MA.registry_path().write_text("{not json", encoding="utf-8")
        self.assertIn("leaseId", self.ws.start(ALPHA))
        with self.ws.http("tok-a"):
            out = self.ws.srv.observatory_recall(projectId=ALPHA)
        self.assertTrue(refused(out, "registry-unreadable"), out)

    def test_an_http_process_has_no_default(self) -> None:
        self.ws.MA.serve_http()
        try:
            out = self.ws.srv.observatory_recall(projectId=ALPHA)
            self.assertTrue(refused(out, "unknown-channel"), out)
        finally:
            self.ws.MA.serve_stdio()


class EveryMemoryToolAsks(Base):
    """A memory tool that forgot its gate would answer here; each must refuse first."""

    CALLS = {
        "observatory_search": dict(query="export", project_id=ALPHA),
        "observatory_recall": dict(projectId=ALPHA),
        "observatory_record": dict(owner="agent:alpha-bot", statement="x", projectId=ALPHA),
        "observatory_workflow_list": dict(projectId=ALPHA),
        "observatory_checkpoint_write": dict(owner="agent:alpha-bot", idempotencyKey="key-x-0001",
                                             stepId="S1", status="in_progress",
                                             body={"goal": "g"}, projectId=ALPHA),
        "observatory_checkpoint_latest": dict(workflowId="wf_0000000000000000"),
        "observatory_handoff_create": dict(owner="agent:alpha-bot", idempotencyKey="key-x-0002",
                                           workflowId="wf_0000000000000000",
                                           to={"provider": "anthropic"}, reason="limit"),
        "observatory_handoff_accept": dict(owner="agent:alpha-bot", idempotencyKey="key-x-0003",
                                           handoffId="handoff:0000000000000000"),
        "observatory_handoff_get": dict(handoffId="handoff:0000000000000000"),
    }

    def test_the_table_and_the_calls_agree(self) -> None:
        self.assertEqual(set(self.CALLS), set(self.ws.MA.TOOLS))

    def test_each_refuses_a_request_without_a_bearer(self) -> None:
        before = (self.ws.count("ledger"), self.ws.count("workflows"))
        for tool, args in self.CALLS.items():
            with self.ws.http(None):
                out = getattr(self.ws.srv, tool)(**args)
            self.assertTrue(refused(out, "no-credential"), f"{tool}: {out}")
            self.assertNotIn("isError", out)
        self.assertEqual((self.ws.count("ledger"), self.ws.count("workflows")), before,
                         "nothing was written")


class ABinding(Base):
    def setUp(self) -> None:
        super().setUp()
        self.b = self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")

    def test_it_reads_and_writes_inside_its_project(self) -> None:
        with self.ws.http("tok-alpha"):
            wf = self.ws.start(ALPHA)
            self.assertIn("leaseId", wf, wf)
            self.assertIn("checkpoint", self.ws.srv.observatory_checkpoint_latest(
                workflowId=wf["workflowId"]))
            self.assertEqual(self.ws.srv.observatory_workflow_list(projectId=ALPHA)["total"], 1)
            self.assertIn("memoryId", self.ws.note(ALPHA))
            self.assertNotIn("error", self.ws.srv.observatory_recall(projectId=ALPHA))
            self.assertNotIn("error", self.ws.srv.observatory_search(query="work",
                                                                     project_id=ALPHA))
        rows = self.ws.journal()
        self.assertTrue(rows and all(r["allowed"] and r["binding"] == "bnd_test0001" for r in rows))
        text = json.dumps(rows)
        self.assertNotIn("tok-alpha", text)
        self.assertNotIn(self.ws.AB.bearer_digest("tok-alpha"), text,
                         "the journal holds neither the bearer nor its digest")

    def test_another_project_is_refused_before_any_write(self) -> None:
        before = (self.ws.count("ledger"), self.ws.count("workflows"))
        with self.ws.http("tok-alpha"):
            self.assertTrue(refused(self.ws.start(BETA), "project-not-bound"))
            self.assertTrue(refused(self.ws.note(BETA), "project-not-bound"))
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=BETA),
                                    "project-not-bound"))
            self.assertTrue(refused(self.ws.srv.observatory_recall(), "project-required"))
        self.assertEqual((self.ws.count("ledger"), self.ws.count("workflows")), before)
        last = self.ws.journal()[-1]
        self.assertFalse(last["allowed"])
        self.assertEqual(last["reason"], "project-required")

    def test_the_target_decides_the_project(self) -> None:
        theirs = self.ws.start(BETA)                                      # the local agent's
        hid = None
        with self.ws.http("tok-alpha"):
            for out in (self.ws.srv.observatory_checkpoint_latest(workflowId=theirs["workflowId"]),
                        self.ws.srv.observatory_checkpoint_write(
                            owner="agent:alpha-bot", idempotencyKey="key-steal-0001",
                            stepId="S2", status="in_progress", body={"goal": "g"},
                            workflowId=theirs["workflowId"], leaseId=theirs["leaseId"]),
                        self.ws.srv.observatory_handoff_create(
                            owner="agent:alpha-bot", idempotencyKey="key-steal-0002",
                            workflowId=theirs["workflowId"], to={"provider": "anthropic"},
                            reason="limit", leaseId=theirs["leaseId"])):
                self.assertTrue(refused(out, "project-not-bound"), out)
            missing = self.ws.srv.observatory_checkpoint_latest(workflowId="wf_00000000000000ff")
        self.assertTrue(refused(missing, "project-not-bound"),
                        "a missing workflow reads like a foreign one to a binding")
        self.assertEqual(self.ws.srv.observatory_checkpoint_latest(
            workflowId="wf_00000000000000ff")["error"], "UnknownWorkflow",
            "the local agent still gets the store's own answer")
        self.assertIsNone(hid)

    def test_owner_must_be_the_principal(self) -> None:
        before = self.ws.count("ledger")
        with self.ws.http("tok-alpha"):
            out = self.ws.note(ALPHA, owner="agent:someone-else")
            out2 = self.ws.start(ALPHA, owner="agent:someone-else")
        self.assertTrue(refused(out, "owner-not-principal"), out)
        self.assertTrue(refused(out2, "owner-not-principal"), out2)
        self.assertEqual(self.ws.count("ledger"), before)

    def test_an_unbound_scope_and_a_higher_effect_are_refused(self) -> None:
        self.ws.bind("agent:reader", [ALPHA], bearer="tok-read", scopes=("memory.read",))
        with self.ws.http("tok-read"):
            self.assertTrue(refused(self.ws.srv.observatory_search(query="x", project_id=ALPHA),
                                    "scope-not-bound"))
            self.assertTrue(refused(self.ws.note(ALPHA, owner="agent:reader"), "scope-not-bound"))
        self.ws.bind("agent:reader2", [ALPHA], bearer="tok-read2", effect="read")
        with self.ws.http("tok-read2"):
            self.assertTrue(refused(self.ws.note(ALPHA, owner="agent:reader2"),
                                    "effect-above-ceiling"))

    def test_revocation_expiry_and_audience_take_effect_at_once(self) -> None:
        with self.ws.http("tok-alpha"):
            self.assertNotIn("error", self.ws.srv.observatory_recall(projectId=ALPHA))
        self.ws.bindings[0]["revokedAt"] = _stamp()
        self.ws.save()
        with self.ws.http("tok-alpha"):
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=ALPHA),
                                    "binding-revoked"))
        self.ws.bind("agent:old", [ALPHA], bearer="tok-old", expires_days=-0.01)
        with self.ws.http("tok-old"):
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=ALPHA),
                                    "binding-expired"))
        self.ws.bind("agent:far", [ALPHA], bearer="tok-far")
        with self.ws.http("tok-far", audience="observatory:other"):
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=ALPHA),
                                    "audience-mismatch"))

    def test_a_restored_older_registry_is_refused_whole(self) -> None:
        with self.ws.http("tok-alpha"):
            self.assertNotIn("error", self.ws.srv.observatory_recall(projectId=ALPHA))
        self.ws.save(revision=5)
        with self.ws.http("tok-alpha"):
            self.ws.srv.observatory_recall(projectId=ALPHA)
        self.ws.save(revision=4)
        with self.ws.http("tok-alpha"):
            out = self.ws.srv.observatory_recall(projectId=ALPHA)
        self.assertTrue(refused(out, "registry-unreadable"), out)
        self.assertIn("older", out["detail"])

    def test_another_callers_retry_never_receives_the_lease(self) -> None:
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha-2")    # same principal
        with self.ws.http("tok-alpha"):
            first = self.ws.start(ALPHA, key="key-shared-0001")
            again = self.ws.start(ALPHA, key="key-shared-0001")
        with self.ws.http("tok-alpha-2"):
            other = self.ws.start(ALPHA, key="key-shared-0001")
        self.assertTrue(again.get("replayed"), "the same caller's retry replays")
        self.assertEqual(again["leaseId"], first["leaseId"])
        self.assertFalse(other.get("replayed"), "another binding's identical request is new")
        self.assertNotEqual(other["leaseId"], first["leaseId"])
        self.assertNotEqual(other["workflowId"], first["workflowId"])


class Narrowing(Base):
    def setUp(self) -> None:
        super().setUp()
        self.ws.bind("service:fabric-hub", [ALPHA, BETA], bearer="tok-hub")

    def test_fabric_projects_narrow_and_never_widen(self) -> None:
        with self.ws.http("tok-hub", fabric_projects=ALPHA):
            self.assertNotIn("error", self.ws.srv.observatory_recall(projectId=ALPHA))
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=BETA),
                                    "project-not-bound"))
        with self.ws.http("tok-hub", fabric_projects=f"{GAMMA}"):
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=GAMMA),
                                    "project-not-bound"), "a header cannot add a project")
        with self.ws.http("tok-hub", fabric_projects="alpha-web; DROP"):
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=ALPHA),
                                    "fabric-projects-invalid"))
        with self.ws.http("tok-hub"):
            self.assertNotIn("error", self.ws.srv.observatory_recall(projectId=BETA),
                             "without the header the binding applies as issued")


class Classes(Base):
    def test_a_public_binding_sees_public_records_only(self) -> None:
        self.ws.note(ALPHA, statement="public fact", classification="public")
        self.ws.note(ALPHA, statement="internal fact", classification="project-internal")
        self.ws.note(ALPHA, statement="secret fact", classification="confidential")
        self.ws.bind("agent:public", [ALPHA], bearer="tok-pub", class_ceiling="public")
        with self.ws.http("tok-pub"):
            out = self.ws.srv.observatory_recall(projectId=ALPHA)
            wf = self.ws.start(ALPHA, owner="agent:public")
        self.assertEqual([r["statement"] for r in out["records"]], ["public fact"])
        self.assertEqual(out["total"], 1, "total counts only what the caller may see")
        self.assertTrue(refused(wf, "class-ceiling-below-workflow"), wf)

    def test_search_filters_before_the_window(self) -> None:
        self.ws.note(ALPHA, statement="exporter design public", classification="public")
        self.ws.note(ALPHA, statement="exporter design secret", classification="confidential")
        self.ws.bind("agent:public", [ALPHA], bearer="tok-pub", class_ceiling="public")
        from store import db as sdb
        from store import indexer
        conn = sdb.connect()
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                indexer.cmd_index(conn, 100)
        finally:
            conn.close()
        with self.ws.http("tok-pub"):
            out = self.ws.srv.observatory_search(query="exporter design", project_id=ALPHA)
        self.assertTrue(all("secret" not in r["statement"] for r in out["results"]), out)
        self.assertEqual(out["total"], 1, "the unseen class takes no place and no count")
        # Since N-009 the class is filtered inside the candidate query, so there is no
        # post-window filter left to report (tests/test_search_scope.py).
        self.assertNotIn("class-filter", [d["source"] for d in out["degraded"]])


class SessionBinding(Base):
    def test_a_session_binding_touches_only_its_workflows(self) -> None:
        mine = self.ws.start(ALPHA, key="key-mine-0001")
        other = self.ws.start(ALPHA, key="key-other-0001")
        self.ws.bind("service:switchboard", [ALPHA], bearer="tok-sess",
                     workflows=[mine["workflowId"]])
        with self.ws.http("tok-sess"):
            listed = self.ws.srv.observatory_workflow_list(projectId=ALPHA)
            self.assertEqual([w["workflowId"] for w in listed["workflows"]], [mine["workflowId"]])
            self.assertEqual(listed["total"], 1)
            self.assertTrue(refused(self.ws.srv.observatory_checkpoint_latest(
                workflowId=other["workflowId"]), "workflow-not-bound"))
            self.assertIn("checkpoint", self.ws.srv.observatory_checkpoint_latest(
                workflowId=mine["workflowId"]))
            self.assertTrue(refused(self.ws.start(ALPHA, owner="service:switchboard",
                                                  key="key-new-0001"), "workflow-required"),
                            "a session binding cannot start new work")


class LocalOnlySurface(Base):
    def test_other_tools_and_resources_serve_stdio_only(self) -> None:
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")

        async def over_http():
            with self.ws.http("tok-alpha"):
                tool = await self.ws.srv.server.call_tool("observatory_findings", {"limit": 1})
                try:
                    await self.ws.srv.server.read_resource("observatory://estate")
                    resource = None
                except PermissionError as exc:
                    resource = json.loads(str(exc))
            return tool, resource
        tool, resource = asyncio.run(over_http())
        self.assertTrue(tool.is_error)
        body = json.loads(tool.content[0].text)
        self.assertEqual(body["code"], "local-only", body)
        self.assertIn("observatory_search", body["hint"], "the refusal names what is reachable")
        self.assertEqual(resource and resource["code"], "local-only")

        async def over_stdio():
            return await self.ws.srv.server.call_tool("observatory_findings", {"limit": 1})
        local = asyncio.run(over_stdio())
        self.assertFalse(local.is_error)
        self.assertIn("findings", json.loads(local.content[0].text))


class OperatorCli(Base):
    def run_cli(self, *argv: str, terminal: bool) -> tuple[int, str, str]:
        import access_binding_cli as cli
        cli._is_terminal = lambda: terminal
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                err.write(str(exc.code))
        return code, out.getvalue(), err.getvalue()

    def test_issue_needs_a_terminal(self) -> None:
        token = self.ws.home / "bearer"
        code, _, err = self.run_cli("issue", "agent:x", "--project", ALPHA, "--token-file",
                                    str(token), terminal=False)
        self.assertNotEqual(code, 0)
        self.assertIn("terminal", err)
        self.assertFalse(token.exists())
        self.assertFalse(self.ws.MA.registry_path().exists())

    def test_issue_show_and_revoke(self) -> None:
        token = self.ws.home / "secrets" / "bearer"
        code, out, _ = self.run_cli("issue", "agent:x", "--project", ALPHA, "--token-file",
                                    str(token), "--effect", "propose", "--scope",
                                    "memory.checkpoint", "--scope", "memory.read", terminal=True)
        self.assertEqual(code, 0, out)
        bearer = token.read_text(encoding="utf-8").strip()
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertNotIn(bearer, out, "the bearer is never printed")
        doc = json.loads(self.ws.MA.registry_path().read_text(encoding="utf-8"))
        self.assertNotIn(bearer, json.dumps(doc), "the registry holds a digest, never a token")
        self.assertEqual(stat.S_IMODE(self.ws.MA.registry_path().stat().st_mode), 0o600)
        bid = doc["bindings"][0]["bindingId"]
        with self.ws.http(bearer):
            self.assertIn("leaseId", self.ws.start(ALPHA, owner="agent:x"))
        code, shown, _ = self.run_cli("show", "--json", terminal=False)
        self.assertEqual(code, 0)
        self.assertNotIn(doc["bindings"][0]["credentialRef"]["digest"], shown)
        self.assertIn("in force", json.loads(shown)["bindings"][0]["state"])
        code, again, err = self.run_cli("issue", "agent:y", "--project", ALPHA, "--token-file",
                                        str(token), terminal=True)
        self.assertNotEqual(code, 0, "an existing token file is never overwritten")
        self.assertEqual(token.read_text(encoding="utf-8").strip(), bearer)
        code, out, _ = self.run_cli("revoke", bid, terminal=True)
        self.assertEqual(code, 0, out)
        with self.ws.http(bearer):
            self.assertTrue(refused(self.ws.srv.observatory_recall(projectId=ALPHA),
                                    "binding-revoked"))

    def test_doctor_names_the_bindings_read_only(self) -> None:
        self.ws.bind("agent:alpha-bot", [ALPHA], bearer="tok-alpha")
        seen = self.ws.home / "store" / self.ws.MA.SEEN_FILE
        seen.unlink(missing_ok=True)
        got = self.ws.MA.status(self.ws.home / "config", self.ws.home / "store")
        self.assertEqual((got["state"], got["inForce"], got["principals"]),
                         ("ok", 1, ["agent:alpha-bot"]))
        self.assertFalse(seen.exists(), "doctor records nothing")
        self.assertNotIn(self.ws.AB.bearer_digest("tok-alpha"), json.dumps(got))


if __name__ == "__main__":
    unittest.main()
