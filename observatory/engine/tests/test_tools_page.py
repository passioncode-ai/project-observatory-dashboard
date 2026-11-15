#!/usr/bin/env python3
"""The Installed tools page (OBS-552, CP-05): which coding agents, skills, plugins and hooks are installed here.

Built from a synthetic harness scan (`store/raw/harness.json`) and synthetic findings, in both languages: per agent
its version, its plugins (enabled, version, newest known, behind first), its skills per scope with their source tier,
its hooks, and each harness finding beside the item it is about. A scan that was never read, or read in part, is
named with its reason and never shown as "nothing installed". Synthetic throughout.
"""
from __future__ import annotations

import ast
import html
import importlib
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import shell                                                                       # noqa: E402
import tooling_view                                                                # noqa: E402
import tools_page                                                                  # noqa: E402
from i18n import Translator                                                        # noqa: E402

EN, RU = Translator("en"), Translator("ru")
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def plugin(name, agent="claude-code", scope="user", version="1.0.0", latest=None, enabled=True, behind=None,
           project=None, key=None):
    return {"name": name, "agent": agent, "scope": scope, "project": project, "projectKey": key, "version": version,
            "enabled": enabled, "marketplace": name.split("@")[-1], "latest": latest, "latestFrom": None,
            "marketplaceUpdated": None, "behind": behind}


HARNESS = {
    "generated_at": NOW,
    "coding_agents": [{"id": "claude-code", "version": "2.1.0"}, {"id": "codex"}, {"id": "aider", "version": "0.80.0"}],
    "skills": [{"name": "notes", "source": "plain", "tier": 1, "key": "plain/notes"},
               {"name": "helper", "source": "plugin:tool@family", "tier": 2, "key": "plugin:tool@family/helper"}],
    "hooks": [{"event": "SessionStart", "matcher": "", "source": "user", "program": "start-hook",
               "command_sha": "aaaaaaaaaaaa"},
              {"event": "PreToolUse", "matcher": "Bash", "source": "plugin:tool@family", "program": "fetcher",
               "command_sha": "bbbbbbbbbbbb"}],
    "degraded": [{"source": "coding-agent catalogue", "reason": "unreadable: installed coding agents not read"}],
    "inventory": {
        "spaces": [{"space": "this-account", "home": "~",
                    "skills": [{"name": "notes", "agent": "claude-code", "scope": "user", "version": "1.0.0"},
                               {"name": "helper", "agent": "claude-code", "scope": "plugin", "version": None,
                                "plugin": "tool@family"},
                               {"name": "release", "agent": "claude-code", "scope": "project", "version": "0.2.0",
                                "project": "alpha-web"},
                               {"name": "review", "agent": "codex", "scope": "user", "version": None}],
                    "plugins": [plugin("other@family", latest="1.0.0", enabled=False, behind=False),
                                plugin("tool@family", latest="1.1.0", behind=True),
                                plugin("tool@family", scope="project", project="alpha-web", key="ab12cd34",
                                       latest="1.1.0", behind=True),
                                plugin("plug@market", agent="codex", version="3.0.0", enabled=None)]}],
        "findings": [{"code": "plugin-behind", "plugin": "tool@family"}],
        "open": [{"finding": "organization-plugin-in-personal-space", "waitsOn": "OBS-359", "reason": "r"}],
        "degraded": [{"source": "codex:config.toml", "code": "unreadable",
                      "reason": "the config could not be read; run `project-observatory full scan-harness`"}]},
}


def finding(fid, type_, subject, severity="info", title_id=None, **args):
    return {"id": fid, "type": type_, "subject": subject, "severity": severity, "title": f"title {fid}",
            "title_id": title_id or "plugin {plugin} is at {installed}, its marketplace publishes {latest}",
            "title_args": args or {"plugin": "tool@family", "installed": "1.0.0", "latest": "1.1.0"}}


FINDINGS = [
    finding("f-behind", "harness.plugin_behind", "harness:plugin-behind:claude-code:tool@family:user::1.0.0:1.1.0"),
    finding("f-risk", "harness.skill_risk", "harness:skill:helper:download-exec:0123456789ab", "warning",
            "skill {skill} downloads and runs code ({host})", skill="helper", host="example.com"),
    finding("f-hook", "harness.hook_risk", "harness:hook:plugin:tool@family:PreToolUse:download-exec:bbbbbbbbbbbb",
            "critical", "a {event} hook from {source} downloads and runs code", event="PreToolUse",
            source="plugin:tool@family"),
    finding("f-changed", "harness.skill_changed", "harness:changed:plain/notes", "info",
            "skill {skill} changed since it was first seen", skill="notes"),
    finding("f-proj", "harness.skill_risk", "harness:skill:alpha-web/release:hidden-unicode:abc", "critical",
            "skill {skill} carries hidden Unicode characters", skill="alpha-web/release"),
    finding("f-codex", "harness.skill_risk", "harness:skill:codex/review:opaque-content:abc", "info",
            "skill {skill} ships opaque or oversized files", skill="codex/review"),
    finding("f-over", "harness.listing_overflow", "harness:claude-code", "warning",
            "{n} skill descriptions do not fit the session's skill listing", n=3),
    finding("f-agents-md", "harness.instruction_long", "harness:file:~/.codex/AGENTS.md", "info",
            "{path} is {lines} lines, and every session reads all of it", path="~/.codex/AGENTS.md", lines=300),
    finding("f-plain", "harness.plaintext_secret", "harness:plaintext:opencode.json", "warning",
            "{file} keeps {n} credentials in plaintext", file="opencode.json", n=2),
    finding("f-other", "heroku.app_down", "heroku:alpha", "critical", "{domain} is on registrar hold",
            domain="alpha.example.com"),
]


def skills_answer(doc: dict) -> dict:
    """What `machine_view.skills_section()` answers for this scan, as the view reads it."""
    inv = doc["inventory"]
    return {"measuredAt": doc["generated_at"], "spaces": inv["spaces"], "findings": inv["findings"],
            "open": inv["open"], "degraded": list(inv["degraded"]),
            "counts": {"skills": 4, "plugins": 4, "findings": 1}}


def view(doc=HARNESS, findings=FINDINGS):
    return tooling_view.build(doc, skills_answer(doc), findings)


class View(unittest.TestCase):
    def test_every_installed_agent_with_its_version_and_the_agents_with_records_first(self):
        v = view()
        ids = [a["id"] for a in v["agents"]]
        self.assertEqual(ids[:2], ["claude-code", "codex"], "the agents whose records were read come first")
        self.assertIn("aider", ids)
        by = {a["id"]: a for a in v["agents"]}
        self.assertEqual(by["claude-code"]["version"], "2.1.0")
        self.assertEqual(by["claude-code"]["name"], "Claude Code")
        self.assertIsNone(by["codex"]["version"], "a version not read stays unknown, never invented")
        self.assertTrue(by["aider"]["installed"])
        self.assertEqual(by["aider"]["plugins"], [])

    def test_plugins_behind_or_with_a_finding_come_first(self):
        plugins = {a["id"]: a for a in view()["agents"]}["claude-code"]["plugins"]
        self.assertEqual([(p["name"], p["scope"]) for p in plugins],
                         [("tool@family", "user"), ("tool@family", "project"), ("other@family", "user")])
        self.assertEqual([f["id"] for f in plugins[0]["findings"]], ["f-behind"])
        self.assertEqual(plugins[1]["findings"], [], "the user install's finding is not the project install's")
        self.assertTrue(plugins[1]["behind"])

    def test_skills_by_scope_carry_their_source_tier_and_findings(self):
        by = {a["id"]: a for a in view()["agents"]}
        skills = by["claude-code"]["skills"]
        self.assertEqual(list(skills), ["user", "project", "plugin"])
        self.assertEqual(skills["user"][0]["tier"], 1)
        self.assertEqual([f["id"] for f in skills["user"][0]["findings"]], ["f-changed"])
        self.assertEqual(skills["plugin"][0]["tier"], 2)
        self.assertEqual([f["id"] for f in skills["plugin"][0]["findings"]], ["f-risk"])
        self.assertEqual(skills["project"][0]["tier"], 1, "a project's own skills are this person's own code")
        self.assertEqual([f["id"] for f in skills["project"][0]["findings"]], ["f-proj"])
        codex = by["codex"]["skills"]["user"][0]
        self.assertIsNone(codex["tier"], "a tier the scan did not record is unknown, not guessed")
        self.assertEqual([f["id"] for f in codex["findings"]], ["f-codex"])

    def test_hooks_and_agent_findings_sit_beside_what_they_are_about(self):
        v = view()
        by = {a["id"]: a for a in v["agents"]}
        hooks = by["claude-code"]["hooks"]
        self.assertEqual([h["program"] for h in hooks], ["fetcher", "start-hook"], "a hook with a finding first")
        self.assertEqual([f["id"] for f in hooks[0]["findings"]], ["f-hook"])
        self.assertEqual([f["id"] for f in by["claude-code"]["findings"]], ["f-over"])
        self.assertEqual([f["id"] for f in by["codex"]["findings"]], ["f-agents-md"])
        self.assertEqual([f["id"] for f in v["other"]], ["f-plain"], "about no one item: listed apart")
        self.assertNotIn("f-other", json.dumps(v), "only the harness's own findings")
        self.assertEqual(v["counts"], {"agents": 3, "plugins": 4, "behind": 2, "skills": 4, "hooks": 2,
                                       "findings": 9})

    def test_a_finding_whose_item_is_gone_is_still_shown(self):
        lost = finding("f-lost", "harness.skill_risk", "harness:skill:gone:concealment:abc", "critical",
                       "skill {skill} carries hidden Unicode characters", skill="gone")
        self.assertIn("f-lost", [f["id"] for f in view(findings=[lost])["other"]])

    def test_degraded_carries_the_scan_and_the_inventory(self):
        sources = [d["source"] for d in view()["degraded"]]
        self.assertIn("codex:config.toml", sources)
        self.assertIn("coding-agent catalogue", sources)

    def test_findings_not_built_are_named_not_read_as_none(self):
        v = tooling_view.build(HARNESS, skills_answer(HARNESS), None)
        self.assertIn("findings", [d["source"] for d in v["degraded"]])


class Unread(unittest.TestCase):
    """No scan, an unreadable one: the page says so with the reason, never "nothing installed"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name).resolve()
        self.home = base / "workspace"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(base),
                                           "USERPROFILE": str(base)}, clear=True)
        self.env.start()
        import workspace
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        for name in ("machine_view", "tooling_view"):
            if name in sys.modules:
                importlib.reload(sys.modules[name])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_no_scan_is_named_with_what_turns_it_on(self):
        import tooling_view as tv
        v = tv.summary([])
        self.assertEqual(v["agents"], [])
        self.assertIn("not-surveyed", [d.get("code") for d in v["degraded"]])
        page = text(tools_page.tools_html({"tooling": v}, EN))
        self.assertIn("The harness scan has not run here yet", page)
        self.assertNotIn("No plugin", page)
        self.assertNotIn("project-observatory", page, "no command for a person to type")

    def test_an_unreadable_scan_is_named(self):
        import paths
        import tooling_view as tv
        (paths.SCRATCH).mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "harness.json").write_text("{")
        v = tv.summary([])
        self.assertIn("unreadable", [d.get("code") for d in v["degraded"]])
        self.assertIn("could not be read", text(tools_page.tools_html({"tooling": v}, EN)))

    def test_a_scan_is_read_from_the_store(self):
        import paths
        import tooling_view as tv
        (paths.SCRATCH).mkdir(parents=True, exist_ok=True)
        (paths.SCRATCH / "harness.json").write_text(json.dumps(HARNESS))
        v = tv.summary(FINDINGS)
        self.assertEqual(v["counts"]["plugins"], 4)
        self.assertEqual(v["measuredAt"], NOW)


class Page(unittest.TestCase):
    def page(self, t=EN, **over):
        return tools_page.tools_html({"tooling": {**view(), **over}}, t)

    def test_per_agent_sections_plugins_first_with_the_behind_row_on_top(self):
        markup = self.page()
        body = text(markup)
        self.assertLess(body.index("Claude Code"), body.index("Codex CLI"))
        self.assertLess(body.index("Codex CLI"), body.index("Aider"))
        claude = markup[markup.index('id="agent-claude-code"'):markup.index('id="agent-codex"')]
        self.assertLess(claude.index("Plugins"), claude.index("Skills"))
        self.assertLess(claude.index("Skills"), claude.index("Hooks"))
        self.assertLess(claude.index("tool@family"), claude.index("other@family"))
        self.assertIn("2.1.0", text(claude))
        self.assertIn("behind", text(claude))
        self.assertIn("1.1.0", text(claude))
        self.assertIn("disabled", text(claude))
        self.assertIn("version not read", text(markup[markup.index('id="agent-codex"'):]))

    def test_skills_are_collapsed_per_scope_with_their_count(self):
        markup = self.page()
        claude = markup[markup.index('id="agent-claude-code"'):markup.index('id="agent-codex"')]
        details = re.findall(r"<details[^>]*>\s*<summary[^>]*>(.*?)</summary>", claude, re.S)
        labels = [text(d) for d in details]
        self.assertTrue(any(l.startswith("User skills") and "1" in l for l in labels), labels)
        self.assertTrue(any(l.startswith("Project skills") for l in labels), labels)
        self.assertTrue(any(l.startswith("Plugin skills") for l in labels), labels)
        # A scope holding a skill with a finding opens by itself, so the finding is seen.
        self.assertRegex(claude, r"<details[^>]*open[^>]*>\s*<summary[^>]*>.*?User skills", )
        self.assertIn("own code", text(claude))
        self.assertIn("pinned marketplace", text(claude))

    def test_findings_sit_beside_their_item_and_link_to_the_findings_page(self):
        markup = self.page()
        self.assertIn('href="findings.html#f-f-behind"', markup)
        self.assertIn('href="findings.html#f-f-hook"', markup)
        body = text(markup)
        self.assertIn("plugin tool@family is at 1.0.0, its marketplace publishes 1.1.0", body)
        self.assertIn("a PreToolUse hook from plugin:tool@family downloads and runs code", body)
        self.assertIn("opencode.json keeps 2 credentials in plaintext", body)

    def test_degraded_sources_are_named_and_no_command_is_shown(self):
        markup = self.page()
        body = text(markup)
        self.assertIn("codex:config.toml", body)
        self.assertIn("the config could not be read", body)
        self.assertNotIn("project-observatory full", body, "a command is cut from a reason a person reads")
        self.assertNotIn("<code", markup)
        self.assertNotIn("—", markup)
        self.assertIn("observatory_machine", body, "the agent's door is named")

    def test_the_page_reads_in_russian(self):
        body = text(self.page(RU))
        self.assertIn("Плагины", body)
        self.assertIn("Скиллы пользователя", body)
        self.assertNotIn("Plugins", body)

    def test_an_agent_whose_records_are_not_read_says_so_never_none(self):
        markup = self.page()
        aider = text(markup[markup.index('id="agent-aider"'):])
        self.assertIn("are not read yet", aider)
        self.assertNotIn("No plugin recorded", aider)
        empty = {**view()["agents"][0], "plugins": [], "skills": {}, "hooks": []}
        claude = text(tools_page.tools_html({"tooling": {"agents": [empty]}}, EN))
        self.assertIn("No plugin recorded for this agent.", claude)
        self.assertIn("No hook recorded for this agent.", claude)


class Shell(unittest.TestCase):
    def test_the_page_sits_after_machine_in_the_system_group(self):
        self.assertIn(("tools", "Installed tools", "tools"), shell.PAGES)
        system = dict((k, names) for k, _l, names in shell.NAV_GROUPS)["system"]
        self.assertEqual(system[system.index("machine") + 1], "tools")
        self.assertEqual(shell.QUESTIONS["tools"],
                         "Which coding agents, skills, plugins and hooks are installed here, and which are out of date.")

    def test_the_machine_page_links_to_it(self):
        import machine_page
        self.assertIn('href="tools.html"', machine_page.machine_html({"machine": {}}))

    def test_the_built_page_renders_the_section_and_carries_no_inventory_in_its_data(self):
        source = ast.parse((ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8"))
        template = next(ast.literal_eval(node.value) for node in source.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in node.targets))
        payload = {"tooling": view()}
        page = shell.page_html(template, "tools", payload)
        self.assertIn('id="tools"', page)
        self.assertIn("Which coding agents, skills, plugins and hooks are installed here", page)
        self.assertNotIn('id="tools"', shell.page_html(template, "machine", payload))
        self.assertIsNone(shell.slice_for("tools", payload)["tooling"], "the page is rendered; its data is not sent twice")

    def test_the_overview_card_counts_without_inventing(self):
        counts = shell.counts_of({})

        def card(payload):
            markup = shell.cards_html(payload, counts)
            return text(re.search(r'<a class="card mod" href="tools.html">.*?</a>', markup, re.S).group(0))
        self.assertIn("not scanned", card({}))
        self.assertIn("3 coding agents · 2 plugins behind", card({"tooling": view()}))


if __name__ == "__main__":
    unittest.main()
