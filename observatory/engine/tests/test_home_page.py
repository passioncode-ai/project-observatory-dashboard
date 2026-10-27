#!/usr/bin/env python3
"""Home (OSS-88, OBS-261): is everything fine, and what needs the person first.

Rendered from synthetic payloads in both languages. The four states are driven:
a fresh install (what to connect, never a row of zeros), a full estate, a source
that failed (its tile says "not measured" with the reason while the others
render), and nothing open ("Nothing needs you" with the time of the check). The
page is static HTML, so its loading state is the live server's refresh and has no
markup of its own here.
"""
from __future__ import annotations

import html
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import home_page                                                                   # noqa: E402
import shell                                                                       # noqa: E402
from i18n import Translator                                                        # noqa: E402

EN, RU = Translator("en"), Translator("ru")


def text(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def tile(markup: str, key: str) -> str:
    m = re.search(r'<(a|div)[^>]*data-tile="' + key + r'"[^>]*>.*?</\1>', markup, re.S)
    assert m, f"no {key} tile"
    return m.group(0)


def finding(fid, type_, severity, first_seen="2026-10-01", action="do it", **extra):
    return {"id": fid, "type": type_, "severity": severity, "first_seen": first_seen, "action": action,
            "title": f"title {fid}", "title_id": "{domain} is on registrar hold", "title_args": {"domain": fid},
            **extra}


def findings(items, built_at="2026-10-26T17:26:00Z"):
    counts = {s: sum(1 for i in items if i["severity"] == s) for s in ("critical", "warning", "info")}
    return {"items": items, "counts": counts, "built_at": built_at,
            "type_labels": {"heroku.app_down": "Heroku app down"}}


def full_payload() -> dict:
    return {
        "runtime": {"home": "/example/workspace", "projects_configured": True},
        "rows": [
            {"id": "project:alpha-web", "name": "alpha-web", "organization": "example-org", "last": "2026-10-26",
             "sites": [{"host": "alpha.example.com",
                        "live": {"resolves": True, "http": 200, "on": "2026-10-26"}}]},
            {"id": "project:beta-api", "name": "beta-api", "organization": "example-org", "last": "2026-10-20",
             "sites": []},
            {"id": "project:gamma", "name": "gamma", "last": "", "sites": []},
        ],
        "domains": [{"name": "beta.example.com", "live": {"resolves": False, "http": 0, "on": "2026-10-25"}},
                    {"name": "unmeasured.example.com", "live": None}],
        "heroku": {"scanned_on": "2026-10-26", "apps": [
            {"name": "a", "state": "running", "crashed": False}, {"name": "b", "state": "running", "crashed": True},
            {"name": "c", "state": "down", "crashed": False}, {"name": "d", "state": "idle", "crashed": False}]},
        "machine": {"measuredAt": "2026-10-26T17:20:00Z", "disk": {"volume": {"free_gb": 8}},
                    "memory": {"total_mb": 16384, "free_mb": 4096, "inactive_mb": 2048}},
        "creds": {"scanned_on": "2026-10-26", "totals": {"leaked_unrotated": 3, "unclaimed": 12}},
        "agents": {"measuredAt": "2026-10-26T17:25:00Z",
                   "counters": {"sessionsActive": 2, "stalled": 1, "workflowsOpen": 4}},
        "findings": findings([
            finding("d1", "domain.hold", "critical", subject="project:alpha-web"),
            finding("h1", "heroku.app_down", "critical", href="heroku.html#a-c", href_label="c"),
            finding("h2", "heroku.app_down", "critical"),
            finding("w1", "repo.stale", "warning", subject="project:beta-api"),
            finding("i1", "note.missing", "info"),
        ]),
    }


class StateBand(unittest.TestCase):
    def test_six_tiles_in_order_each_with_its_time_and_measured_ones_link(self):
        out = home_page.home_html(full_payload(), EN)
        self.assertEqual(re.findall(r'data-tile="([a-z]+)"', out), list(home_page.TILES))
        sites = text(tile(out, "sites"))
        self.assertIn("1 up", sites)
        self.assertIn("1 down", sites, "a host that does not resolve is down; an unmeasured host is not counted")
        self.assertIn("checked 2026-10-26", sites)
        apps = text(tile(out, "apps"))
        self.assertIn("1 up", apps)
        self.assertIn("2 down", apps, "a crashed app is down even while its state says running")
        self.assertIn("read 2026-10-26", apps)
        disk = text(tile(out, "disk"))
        self.assertIn("8 GB free", disk)
        self.assertIn("memory 10 of 16 GB", disk)
        self.assertIn("measured 2026-10-26 17:20 UTC", disk)
        self.assertIn('data-state="bad"', tile(out, "disk"), "8 GB is under the guard's critical line")
        keys = text(tile(out, "keys"))
        self.assertIn("3 leaked, not rotated", keys)
        self.assertIn("12 without a project", keys)
        self.assertIn('href="creds.html?f=c-leaked"', tile(out, "keys"))
        agents = text(tile(out, "agents"))
        self.assertIn("2 sessions active in the last hour", agents)
        self.assertIn("1 workflow stalled now", agents)
        self.assertIn("as of this page's measurement", agents)
        for key, page in (("sites", "domains"), ("apps", "heroku"), ("disk", "machine"), ("agents", "agents")):
            self.assertRegex(tile(out, key), r'^<a[^>]*href="' + page + r'\.html')

    def test_a_source_that_did_not_run_says_not_measured_never_zero_or_green(self):
        payload = {"rows": [{"id": "project:alpha-web", "name": "alpha-web", "sites": []}]}
        out = home_page.home_html(payload, EN)
        for key in ("sites", "apps", "disk", "keys", "agents"):
            with self.subTest(tile=key):
                cell = tile(out, key)
                self.assertIn("not measured", text(cell))
                self.assertIn('data-state="unmeasured"', cell)
                self.assertNotRegex(text(cell), r"\b0 (up|down|leaked|sessions)")

    def test_a_failed_source_keeps_its_tile_with_the_reason_and_the_rest_render(self):
        payload = full_payload()
        payload["heroku"] = {"scanned_on": "2026-10-26", "apps": [],
                             "degraded": [{"reason": "the provider refused the token"}]}
        out = home_page.home_html(payload, EN)
        apps = text(tile(out, "apps"))
        self.assertIn("not measured", apps)
        self.assertIn("the provider refused the token", apps)
        self.assertIn("not measured since 2026-10-26", apps)
        self.assertIn("3 leaked, not rotated", text(tile(out, "keys")))

    def test_the_personal_cloud_is_not_connected_and_no_link(self):
        cloud = tile(home_page.home_html(full_payload(), EN), "cloud")
        self.assertTrue(cloud.startswith("<div"))
        self.assertIn('data-state="unmeasured"', cloud)
        self.assertIn("not connected", text(cloud))


class NeedsYouFirst(unittest.TestCase):
    def test_three_kinds_ranked_one_row_per_kind_with_its_count(self):
        out = home_page.needs_html(full_payload(), EN)
        rows = re.findall(r'<li class="f" data-type="([^"]+)"', out)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2], "repo.stale", "a warning ranks after every critical")
        self.assertEqual(sorted(rows[:2]), ["domain.hold", "heroku.app_down"])
        self.assertIn("open: 2", text(out))
        self.assertIn("Heroku app down", text(out))
        self.assertIn("All problems (5)", text(out))
        self.assertIn('<p class="home-more"><a href="findings.html">', out)
        self.assertIn('href="findings.html#f-d1"', out)
        self.assertIn('href="heroku.html#a-c"', out, "the kind's lead row opens its subject")

    def test_ranking_order_severity_then_actionable_then_age_then_projects(self):
        rank = lambda items: [g["type"] for g in home_page.first_problems(findings(items), limit=10)]
        self.assertEqual(rank([finding("a", "t.info", "info"), finding("b", "t.warn", "warning")]),
                         ["t.warn", "t.info"])
        self.assertEqual(rank([finding("a", "t.noact", "critical", action=""),
                               finding("b", "t.act", "critical", first_seen="2026-10-20")]),
                         ["t.act", "t.noact"], "an action the person can take now beats age")
        self.assertEqual(rank([finding("a", "t.new", "critical", first_seen="2026-10-20"),
                               finding("b", "t.old", "critical", first_seen="2026-09-01")]),
                         ["t.old", "t.new"])
        self.assertEqual(rank([finding("a", "t.one", "critical", subject="project:a"),
                               finding("b", "t.two", "critical", subject="project:b"),
                               finding("c", "t.two", "critical", href="projects.html#project:c")]),
                         ["t.two", "t.one"], "more projects touched ranks first at equal age")

    def test_nothing_open_says_nothing_needs_you_with_the_time_of_the_check(self):
        payload = {"findings": findings([])}
        out = text(home_page.needs_html(payload, EN))
        self.assertIn("Nothing needs you", out)
        self.assertIn("last checked 2026-10-26 17:26 UTC", out)

    def test_never_built_findings_hand_over_the_command(self):
        out = home_page.needs_html({"runtime": {"home": "/example/workspace"}}, EN)
        self.assertIn("Problems not checked yet", text(out))
        self.assertIn("data-copy=\"OBSERVATORY_HOME='/example/workspace' project-observatory full local\"",
                      html.unescape(out))


class ProjectsAtAGlance(unittest.TestCase):
    def test_grouped_by_organization_with_need_and_last_activity(self):
        groups = home_page.project_groups(full_payload())
        self.assertEqual([(g["organization"], g["projects"], g["need"], g["last"]) for g in groups],
                         [("example-org", 2, 2, "2026-10-26"), ("", 1, 0, "")])
        out = text(home_page.projects_html(full_payload(), EN))
        self.assertIn("example-org 2 projects", out)
        self.assertIn("2 need you · last activity 2026-10-26", out)
        self.assertIn("No organization 1 project", out)
        self.assertIn("no activity recorded", out)


class States(unittest.TestCase):
    def test_a_fresh_install_shows_what_to_connect_and_no_tiles(self):
        out = home_page.home_html({"runtime": {"projects_configured": False}, "rows": []}, EN)
        self.assertNotIn("data-tile=", out)
        self.assertIn("Connect your first source", text(out))
        for cmd in ("configure sources projects PATH", "full local", "full doctor", "full agent install"):
            self.assertIn(cmd, out)

    def test_russian(self):
        out = text(home_page.home_html(full_payload(), RU))
        for part in ("Сайты", "1 работает", "Диск и память", "свободно 8 ГБ", "3 утекли и не сменены",
                     "2 сессии активны за последний час", "1 задача стоит сейчас", "Личное облако", "не подключено",
                     "Требует вас в первую очередь", "Все проблемы (5)", "открыто: 2",
                     "Проекты одним взглядом", "Без организации"):
            self.assertIn(part, out)


class Shell(unittest.TestCase):
    def test_home_is_the_index_page_inside_main_before_activity(self):
        import ast
        source = ast.parse((ROOT / "dashboard/build_dashboard.py").read_text())
        template = next(ast.literal_eval(node.value) for node in source.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in node.targets))
        page = shell.page_html(template, "index", full_payload())
        self.assertIn('data-t="Home"', page)
        main = page[page.index('<main id="workspace"'):]
        self.assertLess(main.index('id="home"'), main.index('id="work"'))
        self.assertLess(main.index('id="home"'), main.index('id="tiles"'))
        self.assertNotIn('id="home"', shell.page_html(template, "projects", full_payload()))
        css = (ROOT / "dashboard/workspace.css").read_text(encoding="utf-8")
        self.assertIn('body[data-page="index"] #findings { display: none; }', css.replace(",\n", ", "))


class ReviewFixes(unittest.TestCase):
    """Findings of the independent review of #377."""

    BAD = "__DATA__<img src=x onerror=alert(1)>"

    def template(self):
        import ast
        source = ast.parse((ROOT / "dashboard/build_dashboard.py").read_text())
        return next(ast.literal_eval(node.value) for node in source.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in node.targets))

    def test_a_title_naming_a_placeholder_never_becomes_a_live_tag_on_any_page(self):
        payload = full_payload()
        payload["findings"]["items"].insert(0, {
            "id": "x1", "type": "secret.leaked", "severity": "critical", "first_seen": "2026-01-01",
            "action": "rotate it", "title": self.BAD})
        payload["findings"]["counts"]["critical"] += 1
        self.assertEqual(home_page.first_problems(payload["findings"])[0]["type"], "secret.leaked",
                         "the planted title ranks in the top three")
        template = self.template()
        for page in shell.NAMES:
            with self.subTest(page=page):
                out = shell.page_html(template, page, payload)
                body = re.sub(r"<script\b.*?</script>", "", out, flags=re.S)
                self.assertNotIn("<img src=x", body)
                self.assertEqual(out.count('"rows":'), 1, "the payload is spliced exactly once")
        home = shell.page_html(template, "index", payload)
        self.assertIn("__DATA__&lt;img src=x onerror=alert(1)&gt;", home)

    def test_fill_placeholders_is_one_pass(self):
        out = shell.fill_placeholders("a __X__ b __Y__ c __KEEP_ME__ __X__",
                                      {"X": lambda: "__Y__", "Y": lambda: "y"})
        self.assertEqual(out, "a __Y__ b y c __KEEP_ME__ __Y__")

    def test_a_stale_guard_never_turns_a_fresh_low_disk_green(self):
        machine = lambda **guard: {"machine": {"measuredAt": "2026-10-27T10:00:00Z",
                                               "disk": {"volume": {"free_gb": 4}}, "guard": guard},
                                   "rows": [{"id": "project:a"}]}
        stale = home_page.disk_tile(machine(at="2026-10-01T10:00:00Z", level="normal"), EN)
        self.assertEqual(stale["state"], "bad", "4 GB measured today is under the critical line")
        fresh = {"machine": {"measuredAt": "2026-10-27T10:00:00Z", "disk": {"volume": {"free_gb": 30}},
                             "guard": {"at": "2026-10-27T10:05:00Z", "level": "critical"}}}
        self.assertEqual(home_page.disk_tile(fresh, EN)["state"], "bad", "a guard run after the measurement counts")
        old = {"machine": {"measuredAt": "2026-10-27T10:00:00Z", "disk": {"volume": {"free_gb": 30}},
                           "guard": {"at": "2026-10-01T10:05:00Z", "level": "critical"}}}
        self.assertEqual(home_page.disk_tile(old, EN)["state"], "ok", "an old guard run is ignored")
        lines = {"machine": {"measuredAt": "2026-10-27T10:00:00Z", "disk": {"volume": {"free_gb": 12}},
                             "diskLevels": {"critical": 15, "low": 40}}}
        self.assertEqual(home_page.disk_tile(lines, EN)["state"], "bad", "the guard's own lines decide")

    def test_each_problem_row_shows_its_action(self):
        out = home_page.needs_html(full_payload(), EN)
        rows = re.findall(r'<li class="f".*?</li>', out, re.S)
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertIn('class="act home-act"', row)
            self.assertIn("do it", text(row))
        payload = full_payload()
        payload["runtime"] = {"python": "/example/python", "engine": "/example/engine"}
        payload["findings"]["items"][0]["action"] = 'run python "$(project-observatory full-path)/tools/vault.py" leaks'
        self.assertIn("run '/example/python' '/example/engine/tools/vault.py' leaks",
                      html.unescape(home_page.needs_html(payload, EN)))

    def test_a_page_subject_label_is_translated(self):
        payload = full_payload()
        payload["findings"]["items"][0].update(href="domains.html?f=d-noproject", href_label="Domains",
                                               href_page=True)
        self.assertIn("→ Домены", text(home_page.needs_html(payload, RU)))

    def test_all_problems_counts_items_when_counts_are_absent(self):
        payload = full_payload()
        payload["findings"].pop("counts")
        self.assertIn("All problems (5)", text(home_page.needs_html(payload, EN)))

    def test_digitalocean_and_cloudflare_join_the_apps_tile(self):
        """#376's infrastructure pages land inside the state band, worded as their cards."""
        payload = full_payload()
        payload["heroku"]["apps"] = [{"name": "a", "state": "running", "crashed": False}]
        out = text(tile(home_page.band_html(payload, EN), "apps"))
        self.assertNotIn("DigitalOcean", out, "a provider never connected adds no line")
        payload["infra_integrations"] = {"digitalocean": True, "cloudflare": True}
        payload["digitalocean"] = {"scanned_on": "2026-10-27", "resources": [{}] * 4,
                                   "totals": {"resources": 4, "by_state": {"off": 1}}}
        payload["cloudflare"] = None
        cell = tile(home_page.band_html(payload, EN), "apps")
        self.assertIn("DigitalOcean: 4 resources · off or down: 1", text(cell))
        self.assertIn("Cloudflare: not connected", text(cell))
        self.assertIn('data-state="warn"', cell, "a resource off and billed warns")
        payload["digitalocean"]["totals"]["by_state"] = {"down": 1}
        self.assertIn('data-state="bad"', tile(home_page.band_html(payload, EN), "apps"))

if __name__ == "__main__":
    unittest.main()
