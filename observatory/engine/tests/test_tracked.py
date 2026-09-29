#!/usr/bin/env python3
"""Declared tracking: the store, its two doors (MCP and CLI) and their parity.

Every repository, host, organization and identity below is invented: the
forges are the public host names, the owners are `example-org`/`team`, and the
self-hosted forge is `git.example.invalid`. Token-shaped strings are assembled
at runtime so no credential shape is written into the repository.
"""
from __future__ import annotations
import asyncio
import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import workspace

PLANTED = "gh" + "p_" + "Q7r" * 12
PASSWORD = "hunter" + "2" * 6


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve() / "workspace"
        env = {"OBSERVATORY_HOME": str(self.home), "PATH": os.environ.get("PATH", ""),
               "HOME": str(Path(self.tmp.name) / "user"),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        workspace.initialize(self.home, identities=False)
        import paths
        importlib.reload(paths)
        import organizations
        importlib.reload(organizations)
        import tracked
        self.t = importlib.reload(tracked)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def config(self, name):
        return self.home / "config" / name

    def doc(self):
        return json.loads(self.config("tracked.json").read_text())

    def registry(self, name, doc):
        (self.home / "registry" / name).write_text(json.dumps(doc))


class Addresses(Base):
    """The URL forms a forge serves, and the ones that are refused."""

    def parse(self, url=None, **kw):
        return self.t.parse_repository(url, **kw)

    def refused(self, code, url=None, **kw):
        with self.assertRaises(self.t.TrackError) as ctx:
            self.parse(url, **kw)
        self.assertEqual(ctx.exception.code, code, ctx.exception.detail)
        return ctx.exception

    def test_github_forms_share_one_key(self):
        for url in ("https://github.com/example-org/alpha-web.git",
                    "https://github.com/example-org/alpha-web",
                    "https://github.com/example-org/alpha-web/",
                    "git@github.com:example-org/alpha-web.git",
                    "ssh://git@github.com/example-org/alpha-web.git"):
            got = self.parse(url)
            self.assertEqual((got["key"], got["host"], got["owner"], got["name"]),
                             ("example-org/alpha-web", "github", "example-org", "alpha-web"), url)

    def test_bitbucket_and_gitlab_and_self_hosted(self):
        bb = self.parse("git@bitbucket.org:team/beta-api.git")
        self.assertEqual((bb["key"], bb["host"]), ("team/beta-api", "bitbucket"))
        gl = self.parse("https://gitlab.com/group/sub/gamma-app.git")
        self.assertEqual((gl["key"], gl["host"], gl["owner"], gl["name"]),
                         ("gitlab.com/group/sub/gamma-app", "gitlab", "group/sub", "gamma-app"))
        sh = self.parse("ssh://git@git.example.invalid:2222/team/delta.git")
        self.assertEqual((sh["key"], sh["host"], sh["hostname"]),
                         ("git.example.invalid/team/delta", "git.example.invalid", "git.example.invalid"))
        self.assertEqual(sh["url"], "ssh://git@git.example.invalid:2222/team/delta.git")
        self.assertEqual(self.parse("http://git.example.invalid/team/delta")["key"],
                         "git.example.invalid/team/delta")

    def test_host_owner_name_builds_the_https_address(self):
        got = self.parse(host="GitHub.com", owner="example-org", name="alpha-web")
        self.assertEqual(got["url"], "https://github.com/example-org/alpha-web.git")
        self.assertEqual(got["key"], "example-org/alpha-web")
        self.refused("invalid-url", "https://github.com/example-org/alpha-web", host="github.com")
        self.refused("invalid-url", host="github.com", owner="example-org")

    def test_credentials_are_refused_and_never_repeated(self):
        for url in (f"https://user:{PASSWORD}@github.com/example-org/alpha-web.git",
                    f"https://{PLANTED}@github.com/example-org/alpha-web.git",
                    f"https://oauth2:{PLANTED}@gitlab.com/group/alpha-web.git",
                    f"ssh://git:{PASSWORD}@git.example.invalid/team/delta.git",
                    f"https://git.example.invalid/team/delta.git?private_token={PLANTED}",
                    f"https://git.example.invalid/team/delta.git#{PLANTED}"):
            err = self.refused("credential-refused", url)
            said = json.dumps(err.answer())
            self.assertNotIn(PLANTED, said, url)
            self.assertNotIn(PASSWORD, said, url)
        # The hint hands back the address without the secret part.
        err = self.refused("credential-refused", f"https://{PLANTED}@github.com/example-org/alpha-web.git")
        self.assertIn("https://github.com/example-org/alpha-web.git", err.hint)

    def test_other_forms_are_refused_with_a_reason(self):
        for url, code in (("file:///srv/git/alpha.git", "invalid-url"),
                          ("/srv/git/alpha.git", "invalid-url"),
                          ("../alpha", "invalid-url"),
                          ("git://git.example.invalid/team/delta.git", "invalid-url"),
                          ("ftp://git.example.invalid/team/delta.git", "invalid-url"),
                          ("http://github.com/example-org/alpha-web", "invalid-url"),
                          ("https://github.com/example-org/sub/alpha-web", "invalid-url"),
                          ("https://github.com/example-org", "invalid-url"),
                          ("https://gitlab.com/alpha-web", "invalid-url"),
                          ("https://github.com/example-org/../alpha", "invalid-url"),
                          ("https://bad_host/team/x", "invalid-url"),
                          ("https://git.example.invalid:99999/team/x", "invalid-url"),
                          ("not a url", "invalid-url"),
                          ("", "invalid-url")):
            self.refused(code, url)

    def test_repository_key_accepts_url_id_or_key(self):
        self.assertEqual(self.t.repository_key("repository:example-org/alpha-web"), "example-org/alpha-web")
        self.assertEqual(self.t.repository_key("git@github.com:example-org/alpha-web.git"), "example-org/alpha-web")
        self.assertEqual(self.t.repository_key("gitlab.com/group/x"), "gitlab.com/group/x")

    def test_slugs(self):
        self.assertEqual(self.t.slug_of("project:alpha-web"), "alpha-web")
        for bad in ("Alpha", "-x", "a b", "", "x" * 65, None):
            with self.assertRaises(self.t.TrackError):
                self.t.slug_of(bad)

    def test_the_caller_rule_is_the_servers(self):
        src = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        pattern = next(n.value.args[0].value for n in ast.walk(tree)
                       if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "CALLER_ID")
        self.assertEqual(pattern, self.t.CALLER_ID.pattern)


class Store(Base):
    """Idempotency, ownership of a declaration, preservation, versioning."""

    def track(self, owner="agent:test", **fields):
        fields.setdefault("url", "https://github.com/example-org/alpha-web.git")
        return self.t.track("repository", fields, owner)

    def test_add_then_same_owner_updates_other_owner_gets_existing(self):
        first = self.track(note="the landing page")
        self.assertEqual(first["status"], "added", first)
        self.assertEqual(first["entry"]["added_by"], "agent:test")
        self.assertIn("added_at", first["entry"])
        self.assertEqual(first["remove"]["cli"],
                         "project-observatory full untrack repository example-org/alpha-web")
        again = self.track(note="the landing page")
        self.assertEqual(again["status"], "unchanged")
        moved = self.track(url="git@github.com:Example-Org/Alpha-Web.git", note="now over ssh",
                           project="alpha")
        self.assertEqual(moved["status"], "updated", moved)
        self.assertEqual((moved["entry"]["note"], moved["entry"]["project"]), ("now over ssh", "alpha"))
        self.assertEqual(moved["entry"]["updated_by"], "agent:test")
        other = self.track(owner="agent:someone-else", note="mine now", project="beta")
        self.assertEqual(other["status"], "exists")
        self.assertEqual(other["entry"]["note"], "now over ssh")
        self.assertEqual(other["entry"]["added_by"], "agent:test")
        self.assertEqual(len(self.doc()["repositories"]), 1)

    def test_unknown_fields_are_preserved(self):
        self.config("tracked.json").write_text(json.dumps({
            "schema_version": 1, "future_section": {"x": 1},
            "repositories": [{"key": "team/beta-api", "host": "bitbucket", "hostname": "bitbucket.org",
                              "owner": "team", "name": "beta-api",
                              "url": "git@bitbucket.org:team/beta-api.git",
                              "added_by": "agent:test", "added_at": "2026-01-01T00:00:00Z",
                              "future_field": "kept"}],
            "projects": []}))
        self.assertEqual(self.track()["status"], "added")
        doc = self.doc()
        self.assertEqual(doc["future_section"], {"x": 1})
        kept = next(r for r in doc["repositories"] if r["key"] == "team/beta-api")
        self.assertEqual(kept["future_field"], "kept")
        self.assertEqual(self.t.track("repository", {"url": "git@bitbucket.org:team/beta-api.git",
                                                     "note": "x"}, "agent:test")["entry"]["future_field"], "kept")

    def test_future_and_broken_files_are_refused_and_left_alone(self):
        for content in (json.dumps({"schema_version": 2, "repositories": []}), "{not json",
                        json.dumps({"schema_version": 1, "repositories": {"a": 1}})):
            self.config("tracked.json").write_text(content)
            got = self.track()
            self.assertEqual(got["error"], "unreadable", got)
            self.assertEqual(self.config("tracked.json").read_text(), content)

    def test_a_credential_is_never_written(self):
        got = self.track(url=f"https://{PLANTED}@github.com/example-org/alpha-web.git")
        self.assertEqual(got["error"], "credential-refused")
        self.assertFalse(self.config("tracked.json").exists())
        got = self.track(note=f"key is {PLANTED}")
        self.assertEqual(got["error"], "credential-refused")
        self.assertFalse(self.config("tracked.json").exists())
        self.assertNotIn(PLANTED, json.dumps(got))

    def test_the_file_is_private_and_atomic(self):
        self.track()
        self.assertEqual(self.config("tracked.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config("").glob(".tracked.json.*.tmp")), [])

    def test_owner_rules(self):
        self.assertEqual(self.track(owner="operator")["error"], "owner-refused")
        self.assertEqual(self.track(owner="Operator ")["error"], "owner-refused")
        ok = self.t.track("repository", {"url": "https://github.com/example-org/alpha-web"},
                          "operator", operator_allowed=True)
        self.assertEqual(ok["entry"]["added_by"], "operator")

    def test_organizations_must_be_configured(self):
        got = self.track(organization="company")
        self.assertEqual(got["error"], "unknown-organization")
        self.config("organizations.json").write_text(json.dumps({"organizations": {
            "company": {"label": "Example Company", "default": True}}, "projects": {}}))
        self.assertEqual(self.track(organization="nobody")["error"], "unknown-organization")
        self.assertEqual(self.track(organization="company")["entry"]["organization"], "company")

    def test_measured_repository_is_declared_but_never_overridden(self):
        self.registry("repositories.json", {"schema_version": 2, "repositories": [
            {"id": "repository:example-org/alpha-web", "name_with_owner": "example-org/alpha-web",
             "host": "github", "discovered_by": "github-api"}]})
        self.registry("relations.json", {"schema_version": 2, "relations": [
            {"id": "r1", "type": "implemented_by", "from": "project:alpha", "to": "repository:example-org/alpha-web",
             "rule": "repository name"}]})
        got = self.track(project="beta")
        self.assertEqual(got["status"], "added")
        self.assertTrue(got["alreadyMeasured"])
        self.assertEqual(got["measuredBy"], "github-api")
        self.assertTrue(any("will not apply" in w for w in got["warnings"]), got["warnings"])

    def test_next_tick_says_what_this_workspace_will_measure(self):
        off = self.track()["nextTick"]
        self.assertEqual(off["measures"], [])
        self.assertIn("git_remotes", off["notMeasured"][0])
        settings = self.config("settings.json")
        doc = json.loads(settings.read_text())
        doc["integrations"]["git_remotes"] = True
        settings.write_text(json.dumps(doc))
        on = self.t.track("repository", {"url": "https://github.com/example-org/beta-api"}, "agent:test")["nextTick"]
        self.assertIn("git ls-remote https://github.com/example-org/beta-api", on["measures"][0])
        self.assertIn("tracked_fetch", on["notMeasured"][0])
        self.assertIn("scan-tracked", on["when"])


class Projects(Base):
    def test_declare_update_and_refuse_measured(self):
        got = self.t.track("project", {"slug": "alpha-web", "name": "Alpha Web",
                                       "description": "The example storefront"}, "agent:test")
        self.assertEqual(got["status"], "added", got)
        self.assertEqual(got["entry"]["name"], "Alpha Web")
        self.assertEqual(got["remove"]["cli"], "project-observatory full untrack project alpha-web")
        other = self.t.track("project", {"slug": "project:alpha-web", "name": "Renamed"}, "agent:other")
        self.assertEqual((other["status"], other["entry"]["name"]), ("exists", "Alpha Web"))
        self.registry("projects.json", {"schema_version": 2, "projects": [
            {"id": "project:beta-api", "anchor": "vault-folder", "name": "beta-api"}]})
        measured = self.t.track("project", {"slug": "beta-api", "name": "Beta"}, "agent:test")
        self.assertEqual(measured["error"], "already-measured")
        self.assertIn("observatory_propose", measured["hint"])
        self.assertEqual(self.t.track("project", {"slug": "Bad Slug"}, "agent:test")["error"], "invalid-slug")


class Untrack(Base):
    def test_removes_only_declarations_and_journals_the_actor(self):
        self.t.track("repository", {"url": "https://gitlab.com/group/gamma"}, "agent:test")
        self.t.track("project", {"slug": "gamma"}, "agent:test")
        self.t.track("repository", {"url": "https://gitlab.com/group/delta", "project": "gamma"}, "agent:test")
        got = self.t.untrack("repository", "https://gitlab.com/group/gamma.git", "agent:other")
        self.assertEqual(got["status"], "removed", got)
        self.assertTrue(any("agent:test" in w for w in got["warnings"]))
        proj = self.t.untrack("project", "project:gamma", "agent:test")
        self.assertEqual(proj["status"], "removed")
        self.assertTrue(any("gitlab.com/group/delta" in w for w in proj["warnings"]), proj["warnings"])
        lines = [json.loads(x) for x in (self.home / "store" / "tracked-journal.jsonl").read_text().splitlines()]
        self.assertEqual([x["action"] for x in lines], ["add", "add", "add", "remove", "remove"])
        self.assertEqual((lines[3]["actor"], lines[3]["previous_owner"]), ("agent:other", "agent:test"))
        self.assertEqual((self.home / "store" / "tracked-journal.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.t.untrack("repository", "gitlab.com/group/gamma", "agent:test")["error"], "not-tracked")

    def test_a_measured_repository_is_not_removable(self):
        self.registry("repositories.json", {"schema_version": 2, "repositories": [
            {"id": "repository:example-org/alpha-web", "name_with_owner": "example-org/alpha-web",
             "host": "github", "discovered_by": "local-remote-only"}]})
        got = self.t.untrack("repository", "example-org/alpha-web", "agent:test")
        self.assertEqual(got["error"], "measured-not-declared", got)
        self.assertIn("repo_status.json", got["hint"])
        self.assertEqual(self.t.untrack("repository", "x", "operator")["error"], "owner-refused")


class Tools(Base):
    """The MCP door: two new tools beside ten unchanged ones."""

    #: The published parameter names of the ten tools that existed before
    #: tracking. Adding tools must not move them — a host that compiled their
    #: schemas keeps calling them.
    EXISTING = {
        "observatory_status": ["scope", "kind", "value", "includeExternal", "asOfScanId", "limit", "cursor"],
        "observatory_project": ["projectId", "timelineLimit"],
        "observatory_credentials": ["projectId"],
        "observatory_machine": ["explainPid"],
        "observatory_timeline": ["projectId", "since", "limit"],
        "observatory_search": ["query", "project_id", "limit"],
        "observatory_recall": ["projectId", "limit", "cursor"],
        "observatory_findings": ["severity", "include_acknowledged"],
        "observatory_record": ["owner", "statement", "why", "projectId", "sessionId", "memoryId",
                               "expectedRevision", "evidence"],
        "observatory_propose": ["owner", "targetId", "patch", "evidence"],
    }

    def server(self):
        sys.path.insert(0, str(ROOT / "mcp"))
        spec = importlib.util.spec_from_file_location("observatory_mcp_server_under_test", ROOT / "mcp/server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def call(self, mod, name, args):
        async def go():
            return await mod.server.call_tool(name, args)
        res = asyncio.run(go())
        # The SDK answers (content, structured) for a dict-returning tool.
        if isinstance(res, tuple):
            res = res[1]
        if hasattr(res, "structured_content") and res.structured_content:
            return res.structured_content
        if isinstance(res, dict):
            return res.get("result", res)
        raise AssertionError(f"unexpected tool result {type(res)}")

    def tools(self, mod):
        return {t.name: t for t in asyncio.run(mod.server.list_tools())}

    def test_existing_tools_are_unchanged_and_two_are_added(self):
        mod = self.server()
        tools = self.tools(mod)
        self.assertEqual(len(tools), 12, sorted(tools))
        for name, params in self.EXISTING.items():
            self.assertIn(name, tools)
            self.assertEqual(sorted(tools[name].input_schema.get("properties", {})), sorted(params), name)
        track = tools["observatory_track"].input_schema
        self.assertEqual(sorted(track["required"]), ["kind", "owner"])
        self.assertEqual(track["properties"]["kind"].get("enum"), ["repository", "project"])
        for p in ("url", "host", "repositoryOwner", "repositoryName", "project", "organization",
                  "note", "slug", "name", "description"):
            self.assertIn(p, track["properties"], p)
        untrack = tools["observatory_untrack"].input_schema
        self.assertEqual(sorted(untrack["required"]), ["kind", "owner", "target"])
        for t in ("observatory_track", "observatory_untrack"):
            desc = tools[t].description or ""
            self.assertIn("observatory_propose", desc, t)

    def test_instructions_count_the_served_tools(self):
        mod = self.server()
        text = mod.server.instructions
        self.assertTrue(text.startswith("Eight tools read and four write."), text[:60])
        self.assertIn("observatory_track", text)
        self.assertIn("observatory_untrack", text)

    def test_track_and_untrack_over_the_tool_door(self):
        mod = self.server()
        got = self.call(mod, "observatory_track", {"owner": "agent:test", "kind": "repository",
                                                   "url": "https://gitlab.com/group/gamma.git",
                                                   "note": "a partner's app"})
        self.assertEqual(got["status"], "added", got)
        self.assertEqual(got["entry"]["added_by"], "agent:test")
        # snake_case aliases, as every other tool accepts.
        again = self.call(mod, "observatory_track", {"owner": "agent:test", "kind": "repository",
                                                     "host": "gitlab.com", "repository_owner": "group",
                                                     "repository_name": "gamma"})
        self.assertEqual(again["status"], "unchanged", again)
        refused = self.call(mod, "observatory_track", {"owner": "operator", "kind": "repository",
                                                       "url": "https://gitlab.com/group/gamma"})
        self.assertEqual(refused["error"], "owner refused")
        proj = self.call(mod, "observatory_track", {"owner": "agent:test", "kind": "project",
                                                    "slug": "gamma", "name": "Gamma"})
        self.assertEqual(proj["status"], "added", proj)
        gone = self.call(mod, "observatory_untrack", {"owner": "agent:test", "kind": "repository",
                                                      "target": "gitlab.com/group/gamma"})
        self.assertEqual(gone["status"], "removed", gone)
        self.assertIn("degraded", gone)


class Parity(Base):
    """By hand or by agent: the CLI runs the same code and answers the same."""

    def cli(self, *args, stdin_tty=False):
        env = {**os.environ, "OBSERVATORY_HOME": str(self.home)}
        return subprocess.run([sys.executable, str(ROOT / "observatory.py"), *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def test_cli_and_tool_share_one_store_and_one_rule(self):
        p = self.cli("track", "--json", "--as", "agent:test", "repository",
                     "git@bitbucket.org:team/beta-api.git", "--project", "beta", "--note", "billing")
        self.assertEqual(p.returncode, 0, p.stderr)
        by_hand = json.loads(p.stdout)
        by_agent = self.t.track("repository", {"url": "https://bitbucket.org/team/beta-api",
                                               "project": "beta", "note": "billing"}, "agent:test")
        self.assertEqual(by_hand["status"], "added")
        self.assertEqual(by_agent["status"], "updated")      # the URL form changed, nothing else
        self.assertEqual({k: v for k, v in by_hand["entry"].items() if k not in ("url",)},
                         {k: v for k, v in by_agent["entry"].items()
                          if k not in ("url", "updated_by", "updated_at")})
        self.assertEqual(sorted(by_hand), sorted(k for k in by_agent))
        bad = self.cli("track", "--json", "--as", "agent:test", "repository",
                       f"https://{PLANTED}@github.com/example-org/alpha-web.git")
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)["error"],
                         self.t.track("repository", {"url": f"https://{PLANTED}@github.com/x/y"}, "agent:test")["error"])
        self.assertNotIn(PLANTED, bad.stdout + bad.stderr)

    def test_without_a_terminal_the_cli_needs_an_identity(self):
        p = self.cli("track", "--json", "repository", "https://github.com/example-org/alpha-web")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["error"], "owner-refused")
        self.assertFalse(self.config("tracked.json").exists())

    def test_list_and_untrack(self):
        self.cli("track", "--as", "agent:test", "project", "alpha", "--name", "Alpha")
        self.cli("track", "--as", "agent:test", "repository", "https://gitlab.com/group/alpha",
                 "--project", "alpha")
        listed = self.cli("tracked", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        doc = json.loads(listed.stdout)
        self.assertEqual([r["key"] for r in doc["repositories"]], ["gitlab.com/group/alpha"])
        self.assertEqual([p["slug"] for p in doc["projects"]], ["alpha"])
        self.assertTrue(any(d["source"] == "scan-tracked" for d in doc["degraded"]))
        human = self.cli("tracked")
        self.assertIn("gitlab.com/group/alpha", human.stdout)
        self.assertIn("not measured yet", human.stdout)
        gone = self.cli("untrack", "--as", "agent:test", "repository", "gitlab.com/group/alpha")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertIn("removed", gone.stdout)
        self.assertEqual(self.doc()["repositories"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
