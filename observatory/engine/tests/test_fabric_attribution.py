#!/usr/bin/env python3
"""OBS-160: Observatory pulls each project's Fabric id from Fabric's hub and keeps it in its own store.

A fake hub (an MCP Streamable HTTP server on 127.0.0.1 with a temporary `hub.json`) stands in for
Fabric's `fabric.access.request`, `fabric.access.status` and `fabric.project.resolve`, implemented
from the text Fabric published for CO-331 (the real tool lands after Fabric 0.3.7). Every case runs
in a temporary workspace; nothing reaches a real Fabric.
"""
from __future__ import annotations

import atexit
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(tempfile.mkdtemp(prefix="obs-fabric-attr-")).resolve()
atexit.register(shutil.rmtree, BASE, True)
for key, sub in (("OBSERVATORY_HOME", "runtime"), ("OBSERVATORY_STATE", "runtime/store"),
                 ("OBSERVATORY_SCRATCH", "runtime/store/raw"), ("OBSERVATORY_REGISTRY", "runtime/registry"),
                 ("OBSERVATORY_DATA", "data")):
    os.environ[key] = str(BASE / sub)
    (BASE / sub).mkdir(parents=True, exist_ok=True)
os.environ["OBSERVATORY_FABRIC_HUB_JSON"] = str(BASE / "fabric" / "hub.json")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import fabric_attribution as fat  # noqa: E402
import fabric_hub  # noqa: E402
import identifiers  # noqa: E402

DOOR = "door-token-for-tests"
BINDING = "binding-credential-for-tests"
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


class FakeHub:
    def __init__(self) -> None:
        self.answers: dict[str, dict] = {}   # path -> resolve answer (or {"refusal": code})
        self.auth: list[tuple[str, str]] = []  # (tool, bearer)
        self.refuse_binding = False
        self.allow = True
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                bearer = (self.headers.get("Authorization") or "")[7:]
                if bearer not in (DOOR, BINDING) or (bearer == BINDING and hub.refuse_binding):
                    self.send_response(401)
                    self.end_headers()
                    return
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                if body["method"] == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}
                else:
                    name, args = body["params"]["name"], body["params"]["arguments"]
                    hub.auth.append((name, bearer))
                    result = hub.tool(name, args, bearer)
                payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "s1")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tool(self, name: str, args: dict, bearer: str) -> dict:
        def ok(value: dict) -> dict:
            return {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}

        def err(code: str) -> dict:
            value = {"error": {"code": code, "message": code}}
            return {**ok(value), "isError": True}

        if name == "fabric.access.request":
            assert bearer == DOOR and args["callee"] == "fabric" and args["capabilities"] == ["project.resolve"]
            return ok({"requestId": "r1", "status": "allowed" if self.allow else "denied", "expiresAt": "x",
                       "pollSecret": "p" * 43, "note": "n"})
        if name == "fabric.access.status":
            return ok({"status": "allowed", "credential": BINDING})
        if name == "fabric.project.resolve":
            if bearer != BINDING:
                return err("access-required")
            answer = self.answers.get(args["path"], {"refusal": "no-project"})
            return err(answer["refusal"]) if "refusal" in answer else ok(answer)
        return err("unknown")

    def write_hub_json(self, *, pid: int | None = None, origin: str | None = None) -> None:
        folder = BASE / "fabric"
        folder.mkdir(exist_ok=True)
        token = folder / "hub-door-token"
        token.write_text(DOOR)
        os.chmod(token, 0o600)
        (folder / "hub.json").write_text(json.dumps({
            "protocol": "fabric-hub/0.1", "origin": origin or self.origin, "mcp": "/mcp",
            "doorTokenFile": str(token), "pid": pid or os.getpid(), "startedAt": "2026-01-01T00:00:00Z"}))

    def close(self) -> None:
        self.server.shutdown()


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def folders(*paths: str) -> list[dict]:
    return [{"path": p, "commonDir": None, "origin": None, "toplevel": None} for p in paths]


class Pull(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeHub()
        self.hub.write_hub_json()
        fabric_hub.forget_binding()
        fabric_hub._pending_file().unlink(missing_ok=True)
        self.root = pathlib.Path(tempfile.mkdtemp(dir=BASE))
        self.a, self.b = str(self.root / "alpha-web"), str(self.root / "beta-api")
        self.f = {"project:alpha-web": folders(self.a), "project:beta-api": folders(self.b)}

    def tearDown(self) -> None:
        self.hub.close()

    def answer(self, uuid: str, name: str = "Alpha") -> dict:
        return {"projectId": uuid, "name": name, "root": self.a, "how": "exact", "remoteKey": None}

    def pull(self, previous: dict | None = None) -> dict:
        return fat.collect(previous or {}, fabric_hub.Resolver, folders=self.f, stamp="2026-01-02T00:00:00Z")

    def test_an_answer_becomes_an_alias_with_provenance(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        doc = self.pull()
        rec = doc["aliases"]["project:alpha-web"]
        self.assertEqual(rec["alias"], "fabric:" + UUID_A)
        self.assertEqual(rec["provenance"]["source"], "fabric")
        self.assertEqual(rec["provenance"]["pid"], os.getpid())
        self.assertEqual(doc["degraded"], [])
        self.assertIn("project:beta-api", doc["unattributed"])
        self.assertEqual(doc["history"][-1]["change"], "added")

    def test_the_binding_goes_only_to_resolve_and_never_into_the_record(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        doc = self.pull()
        used = {name: bearer for name, bearer in self.hub.auth}
        self.assertEqual(used["fabric.access.request"], DOOR)
        self.assertEqual(used["fabric.project.resolve"], BINDING)
        self.assertNotIn(BINDING, json.dumps(doc))
        stored = fabric_hub.binding_file()
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)

    def test_a_folder_in_no_project_is_asked_again_and_never_created(self) -> None:
        self.pull()
        asked = sum(1 for n, _b in self.hub.auth if n == "fabric.project.resolve")
        self.pull(self.pull())
        again = sum(1 for n, _b in self.hub.auth if n == "fabric.project.resolve")
        self.assertGreater(again, asked)
        self.assertNotIn("fabric.project.create", {n for n, _b in self.hub.auth})

    def test_detached_withdraws_and_a_new_uuid_replaces(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        first = self.pull()
        first["folders"] = {}  # nothing cached: ask again
        self.hub.answers[self.a] = self.answer(UUID_B)
        second = self.pull(first)
        self.assertEqual(second["aliases"]["project:alpha-web"]["alias"], "fabric:" + UUID_B)
        self.assertEqual(second["history"][-1]["change"], "replaced")
        second["folders"] = {}
        self.hub.answers[self.a] = {"refusal": "detached"}
        third = self.pull(second)
        self.assertNotIn("project:alpha-web", third["aliases"])
        self.assertEqual(third["history"][-1]["change"], "withdrawn")
        self.assertEqual([h["change"] for h in third["history"]], ["added", "replaced", "withdrawn"])

    def test_two_fabric_projects_are_a_conflict_never_a_merge(self) -> None:
        other = str(self.root / "alpha-web-2")
        self.f["project:alpha-web"] = folders(self.a, other)
        self.hub.answers[self.a] = self.answer(UUID_A)
        self.hub.answers[other] = self.answer(UUID_B)
        doc = self.pull()
        self.assertNotIn("project:alpha-web", doc["aliases"])
        self.assertEqual(doc["conflicts"][0]["fabric"], ["fabric:" + UUID_A, "fabric:" + UUID_B])

    def test_an_absent_hub_keeps_what_was_recorded(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        first = self.pull()
        first["folders"] = {}
        (BASE / "fabric" / "hub.json").unlink()
        doc = self.pull(first)
        self.assertEqual(doc["aliases"]["project:alpha-web"]["alias"], "fabric:" + UUID_A)
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-absent"])

    def test_a_dead_pid_sends_nothing(self) -> None:
        self.hub.write_hub_json(pid=dead_pid())
        doc = self.pull()
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-dead"])
        self.assertEqual(self.hub.auth, [])

    def test_an_origin_other_than_loopback_is_refused(self) -> None:
        port = self.hub.origin.rsplit(":", 1)[1]
        for origin in (f"http://localhost:{port}", f"https://127.0.0.1:{port}", f"http://10.0.0.1:{port}"):
            self.hub.write_hub_json(origin=origin)
            doc = self.pull()
            self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-origin-refused"], origin)
        self.assertEqual(self.hub.auth, [])

    def test_a_refused_credential_is_forgotten_and_asked_for_again(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        self.pull()
        self.hub.refuse_binding = True
        doc = self.pull({"folders": {}})
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["access-refused"])
        self.assertFalse(fabric_hub.binding_file().exists())

    def test_a_denied_request_is_named_and_not_asked_again_every_tick(self) -> None:
        self.hub.allow = False
        doc = self.pull()
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["access-refused"])
        self.pull()
        self.assertEqual(sum(1 for n, _b in self.hub.auth if n == "fabric.access.request"), 1)
        self.hub.write_hub_json(pid=os.getppid())  # Fabric restarted: asked again at once
        self.pull()
        self.assertEqual(sum(1 for n, _b in self.hub.auth if n == "fabric.access.request"), 2)

    def test_a_typed_refusal_is_recorded_per_folder(self) -> None:
        for code in ("no-project", "ambiguous", "too-broad", "detached", "bad-path"):
            self.hub.answers[self.a] = {"refusal": code}
            doc = self.pull()
            self.assertEqual(doc["folders"][self.a]["refusal"], code)


class SessionStart(unittest.TestCase):
    def test_fabric_is_asked_first_and_its_answer_mapped_through_the_recorded_alias(self) -> None:
        hub = FakeHub()
        self.addCleanup(hub.close)
        hub.write_hub_json()
        fabric_hub.forget_binding()
        folder = str(BASE / "session-folder")
        os.makedirs(folder, exist_ok=True)
        hub.answers[fat._real(folder)] = {"projectId": UUID_A, "name": "Alpha", "root": folder, "how": "exact",
                                          "remoteKey": None}
        fat.write({"aliases": {"project:alpha-web": {"alias": "fabric:" + UUID_A}}})
        # No credential yet: the session never asks for access, it answers by the local rules.
        self.assertNotEqual(fat.session_project(folder).get("source"), "fabric")
        self.assertEqual([n for n, _b in hub.auth], [])
        fabric_hub.Resolver().resolve(folder)  # the tick's pass obtains the credential
        self.assertEqual(fat.session_project(folder), {"source": "fabric", "project": "project:alpha-web"})


class Resolve(unittest.TestCase):
    def registry(self, attribute=None) -> identifiers.Registry:
        projects = [{"id": "project:alpha-web", "name": "alpha-web"}, {"id": "project:beta-api", "name": "beta-api"}]
        return identifiers.Registry(projects, [], [], {}, [],
                                    fabric_aliases={"project:alpha-web": {"alias": "fabric:" + UUID_A}},
                                    attribute=attribute)

    def test_a_fabric_alias_resolves_and_is_listed(self) -> None:
        out = identifiers.resolve("fabric:" + UUID_A, self.registry())
        self.assertEqual(out["canonical"], "project:alpha-web")
        self.assertEqual(out["matched"]["via"], "alias")
        self.assertIn({"id": "fabric:" + UUID_A, "owner": "fabric"}, out["aliases"])
        self.assertEqual(identifiers.resolve("fabric:" + UUID_B, self.registry())["error"], "not-found")

    def test_folders_resolve_by_the_contract_rules(self) -> None:
        root = pathlib.Path(tempfile.mkdtemp(dir=BASE)).resolve()
        repo = root / "alpha-web"
        (repo / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        link = root / "alpha-link"
        link.symlink_to(repo)
        known = {"project:alpha-web": [{"path": repo.as_posix(), "commonDir": (repo / ".git").as_posix(),
                                        "origin": None, "toplevel": repo.as_posix()}],
                 "project:beta-api": []}
        # The test's folders live under the OS temp root, which the contract refuses (AT-4); a real
        # estate's do not, so the temporary roots are taken out for this case only.
        saved, fat.temp_roots = fat.temp_roots, lambda: []
        self.addCleanup(setattr, fat, "temp_roots", saved)
        reg = self.registry(lambda path: fat.attribute_local(path, known))
        for query in (str(repo), str(repo / "src"), str(link)):
            out = identifiers.resolve(query, reg)
            self.assertEqual(out.get("canonical"), "project:alpha-web", query)
            self.assertEqual(out["matched"]["via"], "folder")
        worktree = root / "_worktrees" / "alpha-feature"
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@example.com", "-c", "user.name=a",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True)
        self.assertEqual(identifiers.resolve(str(worktree), reg).get("canonical"), "project:alpha-web")
        out = identifiers.resolve(str(root / "elsewhere"), self.registry(lambda p: fat.attribute_local(p, known)))
        self.assertEqual(out["error"], "not-found")


class Containers(unittest.TestCase):
    def test_underscore_folders_and_exclusions_are_declared_containers(self) -> None:
        data = pathlib.Path(os.environ["OBSERVATORY_DATA"])
        (data / "_worktrees").mkdir(exist_ok=True)
        (data / "scratch-area").mkdir(exist_ok=True)
        (data / "alpha-web").mkdir(exist_ok=True)
        config = pathlib.Path(os.environ["OBSERVATORY_HOME"]) / "config"
        config.mkdir(exist_ok=True)
        (config / "folder_exclusions.json").write_text(json.dumps(
            {"schema_version": 1, "prefixes": [{"prefix": "scratch-", "why": "t"}], "names": []}))
        found = fat.containers()
        self.assertIn(fat._real(data / "_worktrees"), found)
        self.assertIn(fat._real(data / "scratch-area"), found)
        self.assertNotIn(fat._real(data / "alpha-web"), found)


if __name__ == "__main__":
    unittest.main()
