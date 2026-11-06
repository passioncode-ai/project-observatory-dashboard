#!/usr/bin/env python3
"""`ops.digest.record` end to end (OBS-330): who sends a product's daily digest, for which project, and what is kept.

* over stdio the project is the one project of the access binding the server runs under (`--binding-token-file`); a
  server without one answers `binding-required`, a binding without the scope or the effect, of several projects,
  expired or revoked is refused, and a self-named owner in an argument is refused by the input schema;
* on the memory-http door the project is the `project` claim of a delegation token minted for project-observatory
  with the scope `ops.digest.record`; a binding's bearer there answers `delegation-required`, a token for another
  capability or another audience `delegation-refused`, a token without a project `project-required`;
* the line is checked against the contract (the schema, FAC-SEM-166/167, the source clause of FAC-SEM-168), its day
  must be closed and within the 400 days kept;
* `service` must be a service the registry attributes to the project (a curated `agent:` name, or the agent row's
  source repository the registry measures as the project's);
* one record per (project, service, source, day): the same body again changes nothing, a different one is the next
  revision, a reader reads the first; days older than 400 are pruned; a damaged month file is never overwritten;
* the token and the bearer never reach the store or the answer.

Every value here is synthetic: tokens are signed by a throwaway key, bearers are short test words.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT))
from test_memory_access import ALPHA, BETA, Workspace, tearDownModule  # noqa: E402,F401
from test_memory_http import Base, Service, free_port  # noqa: E402
from test_meter import Estate, ISSUER, claims  # noqa: E402

import ops_digest as OD  # noqa: E402

TOOL = "ops.digest.record"


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def daily(days_ago: int = 1, failed: int = 12, op: str = "board.tick") -> dict:
    """A real daily line for a closed day, folded by the engine's port of the contract's folds."""
    day = (utc_today() - timedelta(days=days_ago)).isoformat()
    hours = [OD.fold_hour([{"op": op, "ok": 3, "failed": failed, "unknown": 0},
                           {"op": "retro-after-work", "ok": 1, "failed": 0, "unknown": 1}], f"{day}T0{h}:00:00Z",
                          None) for h in (1, 3)]
    return OD.fold_day(day, hours)


def structured(result) -> dict:
    out = dict(result.structured_content) if result.structured_content is not None \
        else json.loads(result.content[0].text)
    out["_isError"] = bool(result.is_error)
    return out


def fresh_registry_readers() -> None:
    """`fabric_attribution` binds `paths` at import; a new workspace must not read the last one's files."""
    for name in ("fabric_attribution", "identifiers"):
        sys.modules.pop(name, None)


def write_registry(projects=(ALPHA, BETA), fabric: dict | None = None, aliases: list | None = None) -> None:
    import paths
    reg = pathlib.Path(paths.REGISTRY)
    (reg / "projects.json").write_text(json.dumps({"projects": [{"id": p} for p in projects]}))
    if aliases is not None:
        (reg / "identifiers.json").write_text(json.dumps({"aliases": aliases}))
    if fabric is not None:
        scratch = pathlib.Path(paths.SCRATCH)
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "fabric-attribution.json").write_text(json.dumps(
            {"aliases": {pid: {"alias": alias} for pid, alias in fabric.items()}}))


class OverStdio(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.ws.MA.serve_stdio()
        fresh_registry_readers()
        os.environ.pop("OBSERVATORY_BINDING_TOKEN_FILE", None)

    def tearDown(self) -> None:
        os.environ.pop("OBSERVATORY_BINDING_TOKEN_FILE", None)

    def bind(self, projects=(ALPHA,), scopes=("ops.digest.record",), effect="propose", bearer="tok-ops",
             **over) -> pathlib.Path:
        self.ws.bind("service:fabric", list(projects), bearer=bearer, scopes=scopes, effect=effect, **over)
        path = self.ws.home / f"{bearer}.token"
        path.write_text(bearer + "\n", encoding="utf-8")
        path.chmod(0o600)
        os.environ["OBSERVATORY_BINDING_TOKEN_FILE"] = str(path)
        return path

    def call(self, args: dict) -> dict:
        return structured(asyncio.run(self.ws.srv.server.call_tool(TOOL, args)))

    def store(self) -> pathlib.Path:
        import paths
        return pathlib.Path(paths.STATE) / "ops-digests"

    def test_without_a_binding_the_answer_is_binding_required(self) -> None:
        out = self.call({"digest": daily()})
        self.assertEqual((out["code"], out["_isError"]), ("binding-required", False), out)
        self.assertFalse(self.store().exists())

    def test_a_bound_server_records_for_its_one_project(self) -> None:
        self.bind()
        line = daily()
        first = self.call({"digest": line})
        self.assertEqual((first["accepted"], first["repeated"], first["revision"], first["project"], first["source"],
                          first["service"], first["day"]), (True, False, 1, ALPHA, "app", None, line["day"]), first)
        again = self.call({"digest": line})
        self.assertEqual((again["repeated"], again["revision"]), (True, 1), again)
        other = daily(failed=13)
        changed = self.call({"digest": other})
        self.assertEqual((changed["repeated"], changed["revision"]), (False, 2), changed)
        back = self.call({"digest": line})
        self.assertEqual((back["repeated"], back["revision"]), (True, 1), back)
        import ops_digest_store as S
        lines = S.first_lines(ALPHA)
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["revisions"], lines[0]["digest"]), (2, line))
        # Another label of the same day is its own record.
        self.assertEqual(self.call({"digest": other, "source": "board"})["revision"], 1)
        stored = json.dumps([json.loads(p.read_text()) for p in self.store().rglob("*.json")])
        self.assertNotIn("tok-ops", stored)
        self.assertIn("binding:bnd_test0001", stored)
        if os.name != "nt":
            for p in self.store().rglob("*.json"):
                self.assertEqual(p.stat().st_mode & 0o077, 0, p)

    def test_the_binding_must_hold_the_scope_and_the_effect(self) -> None:
        self.bind(scopes=("memory.read",))
        out = self.call({"digest": daily()})
        self.assertEqual(out["code"], "binding-refused", out)
        self.assertIn("scope-not-bound", out["detail"])
        self.bind(bearer="tok-read", effect="read")
        out = self.call({"digest": daily()})
        self.assertEqual(out["code"], "binding-refused", out)
        self.assertIn("effect-above-ceiling", out["detail"])

    def test_a_binding_of_several_projects_is_refused(self) -> None:
        self.bind(projects=(ALPHA, BETA))
        self.assertEqual(self.call({"digest": daily()})["code"], "binding-not-one-project")

    def test_an_expired_or_unknown_bearer_is_refused(self) -> None:
        self.bind(expires_days=-1)
        out = self.call({"digest": daily()})
        self.assertEqual((out["code"], out["detail"]), ("binding-refused", "binding-expired"), out)
        path = self.ws.home / "stranger.token"
        path.write_text("tok-stranger", encoding="utf-8")
        path.chmod(0o600)
        os.environ["OBSERVATORY_BINDING_TOKEN_FILE"] = str(path)
        self.assertEqual(self.call({"digest": daily()})["detail"], "no-binding")

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_a_token_file_others_can_read_is_refused(self) -> None:
        self.bind().chmod(0o644)
        self.assertEqual(self.call({"digest": daily()})["code"], "binding-unreadable")

    def test_a_self_named_owner_or_project_is_not_an_argument(self) -> None:
        self.bind()
        for extra in ({"owner": "service:fabric"}, {"project": BETA}, {"projectId": BETA}):
            out = self.call({"digest": daily(), **extra})
            self.assertTrue(out["_isError"], out)
        self.assertFalse(self.store().exists())

    def test_the_line_is_checked_against_the_contract(self) -> None:
        self.bind()
        line = daily()
        line["total"]["ok"] += 1
        out = self.call({"digest": line})
        self.assertEqual(out["code"], "digest-refused", out)
        self.assertEqual([f["code"] for f in out["findings"]], ["FAC-SEM-166"])
        line = daily()     # the folds refuse such a name, so it is written into a folded line by hand
        line["ops"][0]["op"] = line["risen"][0]["op"] = "api.users/list"
        out = self.call({"digest": line})
        self.assertEqual({f["code"] for f in out["findings"]}, {"FAC-SEM-167"}, out)
        self.assertNotIn("api.users/list", json.dumps(out))
        # The closed schema: a member it does not name, a log line's text among them, is malformed input.
        self.assertTrue(self.call({"digest": {**daily(), "note": "a log line"}})["_isError"])
        self.assertTrue(self.call({"digest": json.dumps(daily())})["_isError"])
        self.assertEqual(self.call({"digest": daily(), "source": "mask-" + "abcdefghijklmnop"})["code"],
                         "source-refused")
        self.assertFalse(self.store().exists())

    def test_the_day_is_closed_and_within_the_days_kept(self) -> None:
        self.bind()
        self.assertEqual(self.call({"digest": daily(days_ago=0)})["code"], "day-not-closed")
        self.assertEqual(self.call({"digest": daily(days_ago=-3)})["code"], "day-not-closed")
        self.assertEqual(self.call({"digest": daily(days_ago=401)})["code"], "day-outside-retention")
        self.assertTrue(self.call({"digest": daily(days_ago=400)})["accepted"])

    def test_old_days_are_pruned_on_every_write(self) -> None:
        import ops_digest_store as S
        self.bind()
        line = daily(days_ago=30)
        S.record(ALPHA, None, "app", line, "binding:test", today=utc_today())
        later = utc_today() + timedelta(days=380)
        S.record(BETA, None, "app", daily(days_ago=1), "binding:test", today=later)
        self.assertEqual(S.first_lines(ALPHA), [])
        self.assertEqual(len(S.first_lines(BETA)), 1)

    def test_a_record_keeps_at_most_its_revisions_and_a_day_its_sources(self) -> None:
        """OBS-330 review M3: a sender looping on new bodies or labels is refused before the month file grows."""
        import ops_digest_store as S
        self.bind()
        for n in range(S.MAX_REVISIONS):
            self.assertTrue(self.call({"digest": daily(failed=10 + n)})["accepted"])
        out = self.call({"digest": daily(failed=99)})
        self.assertEqual(out["code"], "revisions-exhausted", out)
        self.assertTrue(self.call({"digest": daily(failed=10)})["repeated"], "a body already kept still answers")
        for n in range(1, S.MAX_SOURCES):
            self.assertTrue(self.call({"digest": daily(), "source": f"s{n}"})["accepted"])
        out = self.call({"digest": daily(), "source": "one-too-many"})
        self.assertEqual(out["code"], "sources-exhausted", out)
        self.assertTrue(self.call({"digest": daily(days_ago=2), "source": "one-too-many"})["accepted"],
                        "another day has its own labels")
        month = next(self.store().rglob("*.json")).read_text(encoding="utf-8")
        self.assertNotIn("\n ", month, "written compact")

    def test_a_prune_that_fails_never_refuses_a_kept_line(self) -> None:
        import ops_digest_store as S
        self.bind()
        real = S._prune_locked

        def broken(today):
            raise KeyError("damaged")
        S._prune_locked = broken
        try:
            self.assertTrue(self.call({"digest": daily()})["accepted"])
        finally:
            S._prune_locked = real
        # A damaged entry in an old month is skipped by the prune, which keeps the month as it is.
        old = self.store() / "alpha-web" / "2000-01.json"
        old.write_text(json.dumps({"schema": S.STORE_SCHEMA, "lines": [1, {"day": None}]}), encoding="utf-8")
        self.assertTrue(self.call({"digest": daily(days_ago=2)})["accepted"])
        self.assertTrue(old.exists())

    @unittest.skipIf(os.name == "nt", "a symbolic link needs a privilege on Windows")
    def test_a_link_planted_as_the_lock_is_refused(self) -> None:
        self.bind()
        self.store().mkdir(parents=True)
        (self.store() / ".lock").symlink_to(self.ws.home / "elsewhere")
        out = self.call({"digest": daily()})
        self.assertEqual(out["code"], "store-unwritable", out)
        self.assertFalse((self.ws.home / "elsewhere").exists())

    def test_issue_refuses_a_digest_binding_of_several_projects(self) -> None:
        import access_binding_cli as C
        token = self.ws.home / "several.token"
        args = C.parser().parse_args(["issue", "service:fabric", "--project", ALPHA, "--project", BETA,
                                      "--scope", "ops.digest.record", "--effect", "propose",
                                      "--token-file", str(token)])
        real = C._is_terminal
        C._is_terminal = lambda: True
        try:
            with self.assertRaisesRegex(SystemExit, "exactly one project"):
                C.cmd_issue(args)
        finally:
            C._is_terminal = real
        self.assertFalse(token.exists())

    def test_a_damaged_month_is_refused_and_kept(self) -> None:
        self.bind()
        line = daily()
        month = self.store() / "alpha-web" / f"{line['day'][:7]}.json"
        month.parent.mkdir(parents=True)
        month.write_text("{not json", encoding="utf-8")
        out = self.call({"digest": line})
        self.assertEqual(out["code"], "store-unreadable", out)
        self.assertEqual(month.read_text(encoding="utf-8"), "{not json")

    def test_a_service_must_be_the_projects(self) -> None:
        import paths
        self.bind()
        reg = pathlib.Path(paths.REGISTRY)
        (reg / "projects.json").write_text(json.dumps({"projects": [{"id": ALPHA}, {"id": BETA}]}))
        (reg / "repositories.json").write_text(json.dumps({"repositories": [
            {"id": "repository:example/alpha-web", "url": "https://github.com/example/alpha-web"}]}))
        (reg / "relations.json").write_text(json.dumps({"relations": [
            {"id": "rel-1", "type": "implemented_by", "from": ALPHA, "to": "repository:example/alpha-web"}]}))
        (reg / "agents.json").write_text(json.dumps({"agents": [
            {"id": "agent:alpha-svc.default", "service_id": "alpha-svc", "instance": "default",
             "source_repository": "https://github.com/example/alpha-web.git"},
            {"id": "agent:beta-svc.default", "service_id": "beta-svc", "instance": "default",
             "source_repository": "https://github.com/example/beta-api"}]}))
        ok = self.call({"digest": daily(), "service": {"id": "alpha-svc", "instance": "default"}})
        self.assertEqual(ok["service"], {"id": "alpha-svc", "instance": "default"}, ok)
        for service in ({"id": "beta-svc", "instance": "default"}, {"id": "nobody", "instance": "default"}):
            out = self.call({"digest": daily(), "service": service})
            self.assertEqual(out["code"], "service-not-of-project", out)
        # A curated name in the identifiers table attributes it too.
        (reg / "identifiers.json").write_text(json.dumps({"aliases": [
            {"id": "agent:beta-svc.default", "resolves_to": ALPHA, "owner": "operator", "source": "test"}]}))
        self.assertTrue(self.call({"digest": daily(), "service": {"id": "beta-svc", "instance": "default"}})
                        ["accepted"])

    def test_the_tool_is_listed_with_self_contained_schemas(self) -> None:
        tools = {t.name: t for t in asyncio.run(self.ws.srv.server.list_tools())}
        tool = tools[TOOL]
        text = json.dumps(tool.input_schema) + json.dumps(tool.output_schema)
        self.assertNotIn(".schema.json#", text)
        self.assertEqual(tool.input_schema["required"], ["digest"])
        self.assertEqual(sorted(tool.input_schema["properties"]), ["digest", "service", "source"])
        self.assertEqual(tool.output_schema["type"], "object")


class TheWaitingDeclaration(unittest.TestCase):
    """`ops.digest.record` is served from `fabric/unreleased-capabilities.json` until the release that publishes its
    schemas declares it in `fabric-agent.json` (a manifest may not pin a tag newer than its release, ENG-018)."""

    def test_it_waits_only_until_its_release(self) -> None:
        import re
        import configuration
        import interop
        manifest = json.loads((ROOT / "fabric-agent.json").read_text(encoding="utf-8"))
        declared = {c["name"] for c in manifest["capabilities"]}
        version = tuple(int(x) for x in configuration.VERSION.split(".")[:3])
        for cap in interop.unreleased_capabilities():
            self.assertNotIn(cap["name"], declared, "declared and waiting: empty the waiting list")
            tags = set(re.findall(r"/(v[0-9]+\.[0-9]+\.[0-9]+)/", json.dumps(cap)))
            self.assertEqual(len(tags), 1, tags)
            tag = tuple(int(x) for x in next(iter(tags))[1:].split("."))
            self.assertGreater(tag, version, (
                f"this release reaches {cap['name']}'s tag: move it from fabric/unreleased-capabilities.json into "
                f"fabric-agent.json (provider.revision + 1, tools/fabric_hash.py), update the probe and revision "
                f"counts (tests/test_docs_current.py, tests/test_conformance_receipt.py) and "
                f"fabric/FABRIC-CONFORMANCE.md"))
            # Its pinned files are bundled and published by the release it names (the profile lock's `releases`).
            for uri in re.findall(r'"(https://raw\.githubusercontent\.com/[^"]+)"', json.dumps(cap)):
                self.assertIsNotNone(interop.bundled(uri), uri)

    def test_it_is_served_with_its_handler(self) -> None:
        import interop
        names = [c["name"] for c in interop.mcp_capabilities()]
        self.assertIn(TOOL, names)
        self.assertNotIn(TOOL, [c["name"] for c in interop.mcp_capabilities(unreleased=False)])


class OverHttp(Base):
    def setUp(self) -> None:
        super().setUp()
        fresh_registry_readers()
        write_registry()
        import paths
        self.estate = Estate()
        (self.ws.home / "jwks.json").write_text(json.dumps(self.estate.jwks))
        (paths.CONFIG / "meter.json").write_text(json.dumps(
            {"issuer": ISSUER, "jwks": str(self.ws.home / "jwks.json")}))

    def token(self, **over) -> str:
        now = int(time.time())
        base = {"scope": TOOL, "effect": ["draft"], "project": ALPHA, "iat": now - 5, "exp": now + 300}
        base.update(over)
        return self.estate.sign(claims(**base))

    def test_a_token_names_the_project(self) -> None:
        token = self.token()
        out = self.call(token, TOOL, {"digest": daily(), "source": "app"})
        self.assertEqual((out.get("accepted"), out.get("project")), (True, ALPHA), out)
        import ops_digest_store as S
        stored = json.dumps([json.loads(p.read_text()) for p in S.store_root().rglob("*.json")])
        self.assertIn("delegation:example-writer", stored)
        self.assertNotIn(token, stored)
        self.assertNotIn(token.split(".")[2], stored)
        hub = self.call("tok-alpha", TOOL, {"digest": daily(days_ago=2)}, extra={"Fabric-Delegation": self.token()})
        self.assertTrue(hub.get("accepted"), hub)

    def test_a_binding_bearer_is_not_a_delegation(self) -> None:
        out = self.call("tok-alpha", TOOL, {"digest": daily()})
        self.assertEqual(out["code"], "delegation-required", out)

    def test_a_token_for_something_else_is_refused(self) -> None:
        for over, code in (({"scope": "grant.check"}, "delegation-refused"),
                           ({"aud": "copylot"}, "delegation-refused"),
                           ({"effect": ["none"]}, "delegation-refused"),
                           ({"project": None}, "project-required"),
                           ({"project": "urn:project:a"}, "project-unknown"),
                           # OBS-330 review M1: a project: claim is taken only when the registry holds it.
                           ({"project": "project:no-such-project"}, "project-unknown")):
            with self.subTest(over=over):
                out = self.call(self.token(**over), TOOL, {"digest": daily()})
                self.assertEqual(out["code"], code, out)
        import ops_digest_store as S
        self.assertFalse(S.store_root().exists() and any(S.store_root().rglob("*.json")))

    def test_a_fabric_id_is_taken_from_fabrics_attribution_alone(self) -> None:
        """OBS-330 review M2: a curated alias may not redirect a fabric: id; Fabric's attribution names it."""
        attributed, curated = "fabric:00000000-0000-4000-8000-0000000000a1", "fabric:00000000-0000-4000-8000-0000000000b2"
        write_registry(fabric={ALPHA: attributed},
                       aliases=[{"id": curated, "resolves_to": BETA, "owner": "agent:x", "source": "a host"},
                                {"id": attributed, "resolves_to": BETA, "owner": "agent:x", "source": "a host"}])
        out = self.call(self.token(project=attributed), TOOL, {"digest": daily()})
        self.assertEqual((out.get("accepted"), out.get("project")), (True, ALPHA), out)
        out = self.call(self.token(project=curated), TOOL, {"digest": daily()})
        self.assertEqual(out["code"], "project-unknown", out)
        import ops_digest_store as S
        self.assertFalse((S.store_root() / "beta-api").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
