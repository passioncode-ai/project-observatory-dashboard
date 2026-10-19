#!/usr/bin/env python3
"""OBS-160: Observatory pulls each project's Fabric id from Fabric's hub and keeps it in its own store.

A fake hub (an MCP Streamable HTTP server on 127.0.0.1 with a temporary `hub.json`) stands in for
Fabric's `fabric.access.request`, `fabric.access.status` and `fabric.project.resolve`, implemented
from the text Fabric published for CO-331 (the real tool lands after Fabric 0.3.7). Every case runs
in a temporary workspace; nothing reaches a real Fabric.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

BASE = pathlib.Path(tmpdir.mkdtemp(prefix="obs-fabric-attr-")).resolve()
for key, sub in (("OBSERVATORY_HOME", "runtime"), ("OBSERVATORY_STATE", "runtime/store"),
                 ("OBSERVATORY_SCRATCH", "runtime/store/raw"), ("OBSERVATORY_REGISTRY", "runtime/registry"),
                 ("OBSERVATORY_DATA", "data")):
    os.environ[key] = str(BASE / sub)
    (BASE / sub).mkdir(parents=True, exist_ok=True)
os.environ["OBSERVATORY_FABRIC_HUB_JSON"] = str(BASE / "fabric" / "hub.json")

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
        self.serve_tool = True          # False: the tool is not on tools/list
        self.unknown_tool_error = False  # True: the SDK's own answer for a tool it does not have
        self.poll_secret = True          # False: an earlier request waits; no poll secret this time
        self.requests = 0
        self.expires = True       # False: the access request's answer names no expiry
        self.malformed = None     # "result" or "error": the hub answers resolve with a non-object
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
                elif body["method"] == "tools/list":
                    names = ["fabric.access.request", "fabric.access.status"] + (
                        ["fabric.project.resolve"] if hub.serve_tool else [])
                    result = {"tools": [{"name": n} for n in names]}
                else:
                    name, args = body["params"]["name"], body["params"]["arguments"]
                    hub.auth.append((name, bearer))
                    result = hub.tool(name, args, bearer)
                reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
                if hub.malformed and body.get("method") == "tools/call" and body["params"]["name"] == "fabric.project.resolve":
                    reply = ({"jsonrpc": "2.0", "id": body["id"], "result": ["not", "an", "object"]}
                             if hub.malformed == "result" else {"jsonrpc": "2.0", "id": body["id"], "error": "boom"})
                payload = json.dumps(reply).encode()
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
            self.requests += 1
            later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
            answer = {"requestId": "r1", "status": "allowed" if self.allow else "denied", "note": "n",
                      **({"expiresAt": later} if self.expires else {})}
            return ok({**answer, "pollSecret": "p" * 43} if self.poll_secret else answer)
        if name == "fabric.access.status":
            return ok({"status": "allowed", "credential": BINDING})
        if name == "fabric.project.resolve":
            if self.unknown_tool_error:
                return {"content": [{"type": "text", "text": "MCP error -32602: Tool fabric.project.resolve not found"}],
                        "isError": True}
            if bearer != BINDING:
                return err("access-required")
            answer = self.answers.get(args["path"], {"refusal": "no-project"})
            return err(answer["refusal"]) if "refusal" in answer else ok(answer)
        return err("unknown")

    def write_hub_json(self, *, pid: int | None = None, origin: str | None = None,
                       started: str | None = None) -> None:
        folder = BASE / "fabric"
        folder.mkdir(exist_ok=True)
        token = folder / "hub-door-token"
        token.write_text(DOOR)
        os.chmod(token, 0o600)
        (folder / "hub.json").write_text(json.dumps({
            "protocol": "fabric-hub/0.1", "origin": origin or self.origin, "mcp": "/mcp",
            "doorTokenFile": str(token), "pid": pid or os.getpid(),
            "startedAt": started or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))

    def close(self) -> None:
        self.server.shutdown()


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def folders(*paths: str) -> list[dict]:
    return [{"path": p, "commonDir": None, "origin": None, "toplevel": None} for p in paths]


# The listener check reads `lsof` and `ps` (POSIX); on Windows the hub cannot be verified and nothing is sent.
VERIFIABLE = os.name != "nt" and shutil.which("lsof") is not None and shutil.which("ps") is not None


class _HubCase(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeHub()
        self.hub.write_hub_json()
        fabric_hub.forget_binding()
        fabric_hub._pending_file().unlink(missing_ok=True)
        self.root = pathlib.Path(tmpdir.mkdtemp())
        self.a, self.b = str(self.root / "alpha-web"), str(self.root / "beta-api")
        self.f = {"project:alpha-web": folders(self.a), "project:beta-api": folders(self.b)}

    def tearDown(self) -> None:
        self.hub.close()

    def answer(self, uuid: str, name: str = "Alpha") -> dict:
        return {"projectId": uuid, "name": name, "root": self.a, "how": "exact", "remoteKey": None}

    def pull(self, previous: dict | None = None) -> dict:
        return fat.collect(previous or {}, fabric_hub.Resolver, folders=self.f, stamp="2026-01-02T00:00:00Z")



@unittest.skipUnless(VERIFIABLE, "the hub's listener is verified with lsof and ps")
class Pull(_HubCase):
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

    def asked(self) -> int:
        return sum(1 for n, _b in self.hub.auth if n == "fabric.project.resolve")

    def test_a_refusal_is_cached_then_asked_again_and_nothing_is_created(self) -> None:
        first = self.pull()
        asked = self.asked()
        second = self.pull(first)
        self.assertEqual(self.asked(), asked)  # within the refusal's TTL: not asked again
        for row in second["folders"].values():
            row["epoch"] -= fat.REFUSAL_SECONDS + 1
        self.pull(second)
        self.assertGreater(self.asked(), asked)
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
        # Fabric restarted (the refusal came from another process): asked again at once.
        pending = json.loads(fabric_hub._pending_file().read_text())
        pending["pid"] = pending["pid"] + 1
        fabric_hub.private_io.write(fabric_hub._pending_file(), json.dumps(pending))
        self.pull()
        self.assertEqual(sum(1 for n, _b in self.hub.auth if n == "fabric.access.request"), 2)

    def test_a_typed_refusal_is_recorded_per_folder(self) -> None:
        for code in ("no-project", "ambiguous", "too-broad", "detached", "bad-path"):
            self.hub.answers[self.a] = {"refusal": code}
            doc = self.pull()
            self.assertEqual(doc["folders"][self.a]["refusal"], code)


@unittest.skipUnless(VERIFIABLE, "the hub's listener is verified with lsof and ps")
class PullMore(_HubCase):
    def test_a_live_pid_that_is_not_the_listener_gets_nothing(self) -> None:
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.kill)
        self.hub.write_hub_json(pid=other.pid)
        doc = self.pull()
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-unverified"])
        self.assertEqual(self.hub.auth, [])

    def test_a_reused_pid_started_after_the_hub_gets_nothing(self) -> None:
        self.hub.write_hub_json(started="2001-01-01T00:00:00Z")
        doc = self.pull()
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-unverified"])
        self.assertEqual(self.hub.auth, [])

    def test_no_proxy_ever_carries_the_bearer(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        saved = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for k in ("http_proxy", "HTTP_PROXY"):
            os.environ[k] = "http://127.0.0.1:9"  # nothing listens there: a proxied request fails
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.pop(k, None)
        doc = self.pull()
        self.assertEqual(doc["aliases"]["project:alpha-web"]["alias"], "fabric:" + UUID_A)

    def test_an_absent_tool_is_named_and_withdraws_nothing(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        first = self.pull()
        first["folders"] = {}
        self.hub.serve_tool = False
        doc = self.pull(first)
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["tool-absent"])
        self.assertEqual(doc["aliases"]["project:alpha-web"]["alias"], "fabric:" + UUID_A)
        self.assertEqual([h["change"] for h in doc["history"]], ["added"])

    def test_the_sdks_unknown_tool_answer_is_tool_absent(self) -> None:
        self.hub.unknown_tool_error = True
        doc = fat.collect({}, lambda deadline: fabric_hub.Resolver(deadline=deadline, check_tools=False),
                          folders=self.f)
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["tool-absent"])

    def test_an_error_outside_the_contract_withdraws_nothing(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        first = self.pull()
        first["folders"] = {}
        self.hub.answers[self.a] = {"refusal": "internal"}
        doc = self.pull(first)
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-error"])
        self.assertEqual(doc["aliases"]["project:alpha-web"]["alias"], "fabric:" + UUID_A)

    def test_ambiguous_is_a_conflict_not_a_silent_withdrawal(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        first = self.pull()
        first["folders"] = {}
        self.hub.answers[self.a] = {"refusal": "ambiguous"}
        doc = self.pull(first)
        self.assertEqual(doc["conflicts"][0]["reason"], "ambiguous")
        self.assertNotIn("project:alpha-web", doc["unattributed"])
        self.assertEqual(doc["history"][-1]["change"], "withdrawn")
        self.assertIn("ambiguous", doc["history"][-1]["reason"])

    def test_a_renamed_project_keeps_its_alias_and_history(self) -> None:
        self.hub.answers[self.a] = self.answer(UUID_A)
        first = self.pull()
        first["folders"] = {}
        self.f = {"project:alpha-site": folders(self.a), "project:beta-api": folders(self.b)}
        doc = self.pull(first)
        self.assertEqual(doc["aliases"]["project:alpha-site"]["alias"], "fabric:" + UUID_A)
        self.assertEqual(doc["history"][-1], {"at": "2026-01-02T00:00:00Z", "project": "project:alpha-site",
                                              "change": "moved", "from": "project:alpha-web",
                                              "alias": "fabric:" + UUID_A})

    def test_a_lost_poll_secret_waits_for_the_request_to_expire_then_asks_again(self) -> None:
        self.hub.poll_secret = False
        doc = self.pull()
        self.assertEqual([d["reason"] for d in doc["degraded"]], ["access-pending"])
        self.pull()
        self.assertEqual(self.hub.requests, 1)  # not asked again while the request waits
        pending = json.loads(fabric_hub._pending_file().read_text())
        pending["expiresAt"] = "2001-01-01T00:00:00Z"
        fabric_hub.private_io.write(fabric_hub._pending_file(), json.dumps(pending))
        self.hub.poll_secret = True
        self.hub.answers[self.a] = self.answer(UUID_A)
        doc = self.pull()
        self.assertEqual(self.hub.requests, 2)
        self.assertEqual(doc["aliases"]["project:alpha-web"]["alias"], "fabric:" + UUID_A)


    def test_a_lost_poll_secret_without_an_expiry_backs_off(self) -> None:
        self.hub.poll_secret, self.hub.expires = False, False
        for _ in range(3):
            doc = self.pull()
            self.assertEqual([d["reason"] for d in doc["degraded"]], ["access-pending"])
        self.assertEqual(self.hub.requests, 1)

    def test_a_malformed_answer_is_a_named_hub_error_and_the_pass_completes(self) -> None:
        for kind in ("result", "error"):
            self.hub.malformed = kind
            doc = self.pull()
            self.assertEqual([d["reason"] for d in doc["degraded"]], ["hub-error"], kind)
            out = BASE / f"malformed-{kind}.json"
            fat.write(doc, out)
            self.assertTrue(out.is_file())

    def test_the_listener_is_verified_in_any_language(self) -> None:
        port = int(self.hub.origin.rsplit(":", 1)[1])
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        saved = {k: os.environ.get(k) for k in ("LC_ALL", "LANG", "LC_TIME")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for loc in ("ru_RU.UTF-8", "de_DE.UTF-8", "en_GB.UTF-8"):
            for k in saved:
                os.environ[k] = loc
            fabric_hub._VERIFIED.clear()
            self.assertEqual(fabric_hub.verify_listener(os.getpid(), port, started), "", loc)


class TrueCase(unittest.TestCase):
    def test_etime_reads_every_form_ps_prints(self) -> None:
        self.assertEqual(fabric_hub._etime_seconds("05:07"), 307)
        self.assertEqual(fabric_hub._etime_seconds("02:05:07"), 7507)
        self.assertEqual(fabric_hub._etime_seconds("3-02:05:07"), 3 * 86400 + 7507)
        self.assertIsNone(fabric_hub._etime_seconds("Mon Oct 19"))

    def test_a_folder_is_spelled_as_the_file_system_holds_it(self) -> None:
        root = pathlib.Path(tmpdir.mkdtemp()).resolve()
        (root / "Alpha-Web").mkdir()
        self.assertEqual(fat._real(root / "ALPHA-WEB"), fat._real(root / "Alpha-Web"))
        self.assertTrue(fat._real(root / "ALPHA-WEB").endswith("/Alpha-Web"))


class Unverifiable(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_refuses_rather_than_trusting_a_pid(self) -> None:
        self.assertTrue(fabric_hub.verify_listener(os.getpid(), 1, "2026-01-01T00:00:00Z"))


class Findings(unittest.TestCase):
    def test_a_conflict_is_a_finding_naming_both_fabric_projects(self) -> None:
        sys.path.insert(0, str(ROOT / "dashboard"))
        from finding_types import titled
        def title(project, count, ambiguous):
            return titled("{project} belongs to {count} Fabric projects", project=project, count=count)
        rows = fat.conflict_findings({"conflicts": [{"project": "project:alpha-web",
                                                     "fabric": ["fabric:" + UUID_A, "fabric:" + UUID_B]}]}, title)
        self.assertEqual(rows[0]["title"], "project:alpha-web belongs to 2 Fabric projects")
        self.assertEqual([r["type"] for r in rows], ["fabric.attribution_conflict"])
        self.assertEqual(rows[0]["subject"], "project:alpha-web")
        self.assertIn("fabric:" + UUID_B, rows[0]["detail"])
        self.assertEqual(fat.conflict_findings({}, title), [])


@unittest.skipUnless(VERIFIABLE, "the hub's listener is verified with lsof and ps")
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
        root = pathlib.Path(tmpdir.mkdtemp()).resolve()
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
            self.assertEqual(out["matched"]["via"], "alias")
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
