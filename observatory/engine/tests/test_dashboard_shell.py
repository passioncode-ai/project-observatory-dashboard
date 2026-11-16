"""UI-02/04/08: real page markup and bounded attention previews, offline."""
from __future__ import annotations

import ast
import copy
import importlib
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dashboard_shell", ROOT / "dashboard/shell.py")
shell = importlib.util.module_from_spec(SPEC)
import sys
sys.path.insert(0, str(ROOT / "dashboard"))
SPEC.loader.exec_module(shell)
RU = shell.Translator("ru")


def text(markup: str) -> str:
    """What a reader sees of a marked fragment: its text, entities decoded."""
    import html, re
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.mains = []
        self.elements = {}
        self.ids = []
        self.links = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "main":
            self.mains.append(attrs)
        if "id" in attrs:
            self.elements[attrs["id"]] = (tag, attrs, tuple(self.stack))
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def findings(items, silenced=None):
    return {"items": items, "counts": {severity: sum(item["severity"] == severity for item in items)
                                       for severity in ("critical", "warning", "info")},
            "silenced": silenced or [], "folded_by_type": {"example": 12}}


class DashboardShellTests(unittest.TestCase):
    def test_all_pages_have_one_main_containing_their_primary_content(self):
        source = ast.parse((ROOT / "dashboard/build_dashboard.py").read_text())
        template = next(ast.literal_eval(node.value) for node in source.body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in node.targets))
        primary = {"index": "home", "findings": "findings", "health": "observer"}
        for page in shell.NAMES:
            with self.subTest(page=page):
                parsed = Markup()
                parsed.feed(shell.page_html(template, page, {}))
                self.assertEqual([m["id"] for m in parsed.mains], ["workspace"])
                self.assertEqual(parsed.mains[0]["tabindex"], "-1")
                self.assertNotIn("hidden", parsed.mains[0])
                self.assertIn(("main", "workspace"), parsed.elements[primary.get(page, "out")][2])
                self.assertEqual(parsed.elements["out"][0], "div")
                self.assertTrue(any(a.get("class") == "skip-link" and a.get("href") == "#workspace"
                                    for a in parsed.links))
                routes = [a for a in parsed.links if a.get("class") == "pg"]
                self.assertEqual({a["href"] for a in routes}, {f"{name}.html" for name in shell.NAMES})
                self.assertEqual([a["href"] for a in routes if a.get("aria-current") == "page"], [f"{page}.html"])
                for group in ("monitoring", "work", "infrastructure", "access", "system"):
                    self.assertIn("nav-" + group, parsed.elements)
                if page == "index":
                    # Home (OSS-88) comes first in reading order, before activity.
                    self.assertLess(parsed.ids.index("home"), parsed.ids.index("tiles"))
                    self.assertLess(parsed.ids.index("home"), parsed.ids.index("work"))

    def test_warning_only_preview_is_not_empty_and_preserves_counts(self):
        payload = {"findings": findings([{"id": "w", "severity": "warning", "detail": "evidence", "folded": True}])}
        preview = shell.slice_for("index", payload)["findings"]
        self.assertEqual([f["id"] for f in preview["items"]], ["w"])
        self.assertEqual(preview["counts"], {"critical": 0, "warning": 1, "info": 0})
        self.assertFalse(preview["items"][0]["folded"])
        self.assertEqual(preview["elsewhere"], 0)

    def test_overflow_is_bounded_visible_ranked_and_does_not_mutate_source(self):
        items = [{"id": "info", "severity": "info", "detail": "info"}]
        items += [{"id": str(i), "severity": "critical", "detail": "evidence", "action": "long action", "folded": i >= 8}
                  for i in range(12)]
        payload = {"findings": findings(items)}
        before = copy.deepcopy(payload)
        preview = shell.slice_for("index", payload)["findings"]
        self.assertEqual([f["id"] for f in preview["items"]], [str(i) for i in range(8)])
        self.assertTrue(all(not f["folded"] and not f["detail"] and not f["action"] for f in preview["items"]))
        self.assertEqual(preview["elsewhere"], 5)
        self.assertEqual(preview["counts"], before["findings"]["counts"])
        self.assertEqual(payload, before)
        self.assertEqual(shell.slice_for("findings", payload)["findings"], before["findings"])

    def test_acknowledged_only_history_survives_and_sensitive_payloads_stay_scoped(self):
        payload = {"findings": findings([], [{"id": "hidden", "acked": {"why": "reviewed"}}]),
                   "keys": {"private-metadata": []}, "remote": {"private-metadata": []},
                   "env": {"files": []}, "creds": {"credentials": []}}
        preview = shell.slice_for("index", payload)
        self.assertEqual(preview["findings"]["silenced"][0]["id"], "hidden")
        self.assertEqual(preview["findings"]["elsewhere"], 0)
        for key in ("keys", "remote", "env", "creds"):
            self.assertIsNone(preview[key])

    def test_summary_routes_have_no_inventory_badge_and_queue_stays_labelled(self):
        payload = {"health": {"proposed": 304},
                   "findings": {"counts": {"critical": 27, "warning": 100, "info": 5}}}
        counts = shell.counts_of(payload)
        self.assertEqual(counts["index"], "")
        self.assertEqual(counts["health"], "")
        self.assertEqual(counts["findings"], 132)
        # The agents' unconfirmed records are no reason to open the page (operator, 2026-10-06).
        self.assertNotIn("awaiting a decision", text(shell.cards_html(payload, counts)))
        self.assertNotIn("ждут решения", text(shell.cards_html(payload, counts, RU)))

    def test_traffic_summary_distinguishes_unknown_zero_and_partial_resource_sum(self):
        # In Russian, which has the plural and grouping rules worth checking.
        line = lambda totals: text(shell._traffic_line(totals, RU))
        self.assertEqual(line({}), "аналитика не сканировалась")
        unknown = line({"google": {"totals": {
            "users_30d": None, "users_30d_unclaimed": None,
            "unknown_properties": 2, "measured_properties": 0}}})
        self.assertIn("не измерена", unknown)
        self.assertIn("без измерения: 2", unknown)
        self.assertNotIn("0 польз.", unknown)
        self.assertNotIn("без проекта", unknown)
        zero = line({"google": {"totals": {
            "users_30d": 0, "users_30d_unclaimed": 0,
            "unknown_properties": 0, "measured_properties": 1}}})
        self.assertIn("0 польз. / 30 дн", zero)
        self.assertIn("сумма по ресурсам", zero)
        self.assertIn("0 без проекта", zero)
        self.assertNotIn("частично", zero)
        partial = line({"google": {"totals": {
            "users_30d": 1200, "users_30d_unclaimed": 400,
            "unknown_properties": 3, "measured_properties": 2}}})
        for part in ("1\u00a0200 польз. / 30 дн", "сумма по ресурсам", "частично",
                     "измерено ресурсов: 2", "без измерения: 3", "400 без проекта"):
            self.assertIn(part, partial)
        self.assertIn("1,200 users / 30 d", text(shell._traffic_line({"google": {"totals": {
            "users_30d": 1200, "unknown_properties": 0}}}, shell.Translator("en"))))


def _builder():
    """`dashboard/build_dashboard.py` as a module, for its pure helpers."""
    spec = importlib.util.spec_from_file_location("build_dashboard_helpers", ROOT / "dashboard/build_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _template() -> str:
    source = ast.parse((ROOT / "dashboard/build_dashboard.py").read_text())
    return next(ast.literal_eval(node.value) for node in source.body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in node.targets))


class AuditFixes(unittest.TestCase):
    """Defects found by walking all eleven pages in a browser (2026-10-02)."""

    def test_every_finding_type_a_rule_can_emit_has_a_label(self):
        import re
        import finding_types
        emitted = set()
        for module in sorted((ROOT / "tools").glob("*findings*.py")):
            emitted |= set(re.findall(r'"type":\s*"([a-z_]+\.[a-z_-]+)"', module.read_text(encoding="utf-8")))
        # `clone.<state>` is built from the declared states, one type per state.
        tree = ast.parse((ROOT / "tools/build_findings.py").read_text(encoding="utf-8"))
        states = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                      and any(getattr(t, "id", "") == "SYNC_FINDINGS" for t in node.targets))
        emitted |= {f"clone.{state}" for state in states}
        self.assertGreater(len(emitted), 100, "the rule modules were not read")
        self.assertEqual(sorted(emitted - set(finding_types.LABELS)), [], "finding types without a label")
        self.assertEqual(finding_types.labels_for(["heroku.app_down", "example.unknown"]),
                         {"heroku.app_down": "Heroku app down"}, "an unknown type is left to its raw id")

    def test_subject_links_point_only_at_rows_that_exist(self):
        import subject_links as sl
        index = sl.build_index(
            projects=[{"id": "project:alpha-web", "local_folders": ["alpha-folder"]},
                      {"id": "project:beta-api"}],
            relations=[{"type": "implemented_by", "from": "project:beta-api", "to": "repository:example-org/beta"},
                       {"type": "implemented_by", "from": "project:alpha-web", "to": "repository:example-org/beta"}],
            domains=[{"name": "alpha.example.com"}], zones=[{"name": "zone.example.com"}],
            apps=[{"id": "heroku:1111-aaaa", "name": "alpha-app"}],
            credentials=[{"id": "credential:vault/alpha-web/prod/API_TOKEN"}, {"id": "credential:machine/file.json"}],
            google_credentials=[{"client_email": "reader@example-org.iam.example.invalid"}],
            env_files=[{"path": "alpha-folder/.env", "variables": [{"name": "API_TOKEN"}]}],
            mcp_servers=[{"agent": "example-agent", "name": "docs"}])
        r = lambda subject: sl.resolve(subject, index)
        self.assertEqual(r("heroku:1111-aaaa"), {"href": "heroku.html#a-alpha-app", "label": "alpha-app"})
        self.assertEqual(r("app:alpha-app")["href"], "heroku.html#a-alpha-app")
        self.assertIsNone(r("heroku:gone"), "an application the scan does not hold has no row")
        self.assertEqual(r("secret:alpha-web/prod/API_TOKEN")["href"], "creds.html#c-vault-alpha-web-prod-API_TOKEN")
        self.assertIsNone(r("secret:alpha-web/prod/OTHER"))
        self.assertEqual(r("credential:machine/file.json")["href"], "creds.html#c-machine-file.json")
        grant = r("credential:ga4 via reader@example-org.iam.example.invalid")
        self.assertEqual(grant["href"], "traffic.html#gc-reader-example-org.iam.example.invalid")
        self.assertIsNone(r("credential:search console via nobody@example.invalid"),
                          "a credential with no rendered row must not become a link")
        self.assertEqual(r("repository:example-org/beta")["href"], "projects.html#project:alpha-web",
                         "a repository links to its owning project, the first by id")
        self.assertIsNone(r("repository:example-org/unclaimed"))
        self.assertEqual(r("clone:alpha-folder")["href"], "projects.html#project:alpha-web")
        self.assertEqual(r("domain:zone.example.com")["href"], "domains.html#d-zone.example.com")
        self.assertIsNone(r("domain:absent.example.com"))
        self.assertEqual(r("project:beta-api")["href"], "projects.html#project:beta-api")
        self.assertIsNone(r("project:gamma"))
        self.assertEqual(r("env:alpha-folder/.env")["href"], "env.html#e-alpha-folder/.env")
        self.assertEqual(r("env:API_TOKEN")["href"], "env.html?f=e-all&q=API_TOKEN")
        self.assertEqual(r("mcp:example-agent/docs")["href"], "mcp.html#m-example-agent/docs")
        self.assertEqual(r("estate:heroku-orphans"), {"href": "heroku.html?f=noproject", "label": "Heroku", "page": True})
        self.assertIsNone(r("mem:0123abcd"))

    def test_population_subjects_name_real_pages_and_chips(self):
        import re
        import subject_links as sl
        template = _template()
        titles = {title for _n, title, _k in shell.PAGES}
        for subject, (href, title) in sl.PAGE_SUBJECTS.items():
            with self.subTest(subject=subject):
                self.assertIn(href.split("?")[0].split("#")[0][:-len(".html")], shell.NAMES)
                self.assertIn(title, titles)
                for chip in re.findall(r"[?&]f=([a-z-]+)", href):
                    self.assertIn(f'data-f="{chip}"', template)
                if "#" in href:
                    self.assertIn(f'id="{href.split("#")[1]}"', template)

    def test_badges_count_what_each_page_shows_when_it_opens(self):
        env = {"totals": {"secrets": 4}, "files": [
            {"kind": "env", "variables": [{"class": "secret"}, {"class": "config"},
                                          {"class": "config", "available_in": ["beta-api"]}]},
            {"kind": "template", "variables": [{"class": "secret"}, {"class": "secret"}]}]}
        mcp = {"totals": {"declarations": 3, "distinct_servers": 2},
               "servers": [{"name": "a"}, {"name": "a"}, {"name": "b"}]}
        counts = shell.counts_of({"env": env, "mcp": mcp})
        self.assertEqual(counts["env"], 2, "live secrets plus values set elsewhere, not every secret")
        self.assertEqual(counts["mcp"], 3, "declarations, the MCP page's own unit")
        cards = text(shell.cards_html({"env": env, "mcp": mcp}, {**counts, "projects": 0, "findings": 0,
                                                                 "domains": 0, "heroku": 0, "creds": 0}))
        # Both numbers from ONE population, the view the ENV page opens on: the
        # card once printed `totals.secrets` (4, templates included) beside a
        # view of 2 — more secrets than variables (audit A40).
        self.assertIn("2 variables · 1 read as a secret", cards)
        self.assertNotIn("4 read as a secret", cards)
        self.assertIn("3 declarations · 2 servers", cards)

    def test_domain_counts_are_the_domains_page_rows(self):
        domains = [{"name": "alpha.example.com", "projects": ["project:alpha-web"]},
                   {"name": "beta.example.com", "projects": []}]
        zones = [{"name": "alpha.example.com"}, {"name": "zone-a.example.com", "project": "project:beta-api"},
                 {"name": "zone-b.example.com"}]
        self.assertEqual(shell.domain_totals(domains, zones), (4, 2))
        self.assertEqual(shell.counts_of({"domains": domains, "zones": zones})["domains"], 4)

    def test_queue_statements_end_at_a_word_with_an_ellipsis(self):
        clip = _builder().clip_words
        self.assertEqual(clip("short statement", 200), "short statement")
        cut = clip("alpha " * 50, 40)
        self.assertTrue(cut.endswith("alpha…"), cut)
        self.assertLessEqual(len(cut), 40)
        self.assertEqual(clip("x" * 80, 20), "x" * 19 + "…", "an unbroken token is cut where it is")

    def test_identical_conclusions_fold_into_one_line(self):
        rows = [{"project_id": "project:alpha-web", "created_at": f"2026-01-0{d}T00:00:00Z",
                 "statement": text_, "state": "observed", "confidence": 0.5}
                for d, text_ in ((9, "same finding"), (8, "same finding"), (7, "other finding"), (6, "same finding"))]
        notes = _builder().fold_notes(rows)["project:alpha-web"]
        self.assertEqual([(n["text"], n["n"], n["at"]) for n in notes],
                         [("same finding", 3, "2026-01-09"), ("other finding", 1, "2026-01-07")])

    def test_machine_survey_time_reads_like_the_header(self):
        import machine_page
        self.assertEqual(machine_page.stamp("2026-01-02T03:04:05Z"), "2026-01-02 03:04:05 UTC")
        self.assertEqual(machine_page.stamp("2026-01-02T03:04:05.123+00:00"), "2026-01-02 03:04:05 UTC")
        self.assertEqual(machine_page.stamp("not a time"), "not a time")
        page = machine_page.machine_html({"machine": {"measuredAt": "2026-01-02T03:04:05Z"}})
        self.assertIn("Machine surveyed 2026-01-02 03:04:05 UTC", text(page))

    def test_overview_cards_say_not_scanned_and_the_machine_card_is_never_blank(self):
        # UX-7/UX-11: an unscanned page read "0 apps" on its card, and the
        # Machine card had no line at all.
        counts = shell.counts_of({})
        cards = text(shell.cards_html({}, counts))
        for page in ("heroku", "creds", "env", "mcp"):
            self.assertNotIn("0 apps", cards)
        self.assertGreaterEqual(cards.count("not scanned"), 4, cards)
        self.assertIn("not surveyed", cards)
        surveyed = {"machine": {"measuredAt": "2026-01-02T03:04:05Z", "processes": {"count": 3},
                                "disk": {"volume": {"free_gb": 120}}}}
        self.assertIn("120 GB free on disk · 3 processes", text(shell.cards_html(surveyed, counts)))
        self.assertIn("свободно на диске: 120 ГБ · 3 процесса", text(shell.cards_html(surveyed, counts, RU)))

    def test_page_script_fixes_from_the_dashboard_walk(self):
        src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
        # UX-12: no space between the label and an empty value ("yet ;").
        self.assertIn('<span id="upd-label" data-t>Measured</span><span class="mono" id="upd"></span>', src)
        self.assertIn('textContent = " " + DATE(D.measured', src)
        # UX-8: a port is an identifier, not a number to group ("47,391").
        self.assertIn("{port: String(H.server_port)", src)
        # UX-9: a lowercase title id reads as a sentence; a {name} first keeps its case.
        self.assertIn("const sentence = (id, text) => /^[a-z]/.test", src)
        # UX-10: the session count is not drawn below AA, and says "sessions".
        self.assertNotIn(".spark-sess { opacity", src)
        self.assertIn('T("{n} sessions", {n: stotal})', src)


class MonitoringNavigation(unittest.TestCase):
    """CP-17 (OBS-551, OSS-120): Monitoring is the first group of the navigation."""

    def test_the_groups_and_their_order(self):
        self.assertEqual([k for k, _l, _n in shell.NAV_GROUPS],
                         ["monitoring", "work", "infrastructure", "access", "system"])
        groups = {k: names for k, _l, names in shell.NAV_GROUPS}
        self.assertEqual(groups["monitoring"], ("health", "security", "availability", "integrations"))
        self.assertEqual(groups["work"], ("index", "projects", "agents", "findings"))
        self.assertEqual(groups["system"], ("machine", "tools", "disk"))
        listed = [n for names in groups.values() for n in names if n not in shell.NAV_LINKS]
        self.assertEqual(sorted(listed), sorted(shell.NAMES), "every page is reachable, once")

    def test_security_and_availability_open_the_findings_page_narrowed(self):
        items = [{"id": "s1", "type": "secret.leaked_unrotated", "severity": "critical"},
                 {"id": "s2", "type": "dep.vulnerable", "severity": "warning"},
                 {"id": "a1", "type": "site.dead", "severity": "warning"},
                 {"id": "o1", "type": "clone.ahead", "severity": "info"}]
        counts = shell.counts_of({"findings": findings(items)})
        self.assertEqual((counts["security"], counts["availability"]), (2, 1))
        nav = shell.nav_html("findings", counts)
        self.assertIn('href="findings.html?group=security"', nav)
        self.assertIn('href="findings.html?group=availability"', nav)
        self.assertLess(nav.index('id="nav-monitoring"'), nav.index('id="nav-work"'))
        self.assertLess(nav.index('href="health.html"'), nav.index('href="findings.html?group=security"'))
        self.assertIn('href="findings.html" aria-current="page"', nav)
        self.assertIn("Безопасность", shell.nav_html("index", counts, RU))
        self.assertEqual(shell.counts_of({})["security"], "", "no findings built is no count, never 0")

    def test_finding_types_belong_to_one_monitoring_group_or_none(self):
        import finding_types
        self.assertEqual(finding_types.group_of("secret.leaked_unrotated"), "security")
        self.assertEqual(finding_types.group_of("ssh.key_weak"), "security")
        self.assertEqual(finding_types.group_of("dep.vulnerable"), "security")
        self.assertEqual(finding_types.group_of("site.dead"), "availability")
        self.assertEqual(finding_types.group_of("heroku.app_down"), "availability")
        self.assertEqual(finding_types.group_of("mcp.unreachable"), "availability")
        self.assertIsNone(finding_types.group_of("clone.ahead"))
        self.assertIsNone(finding_types.group_of(None))
        named = [t for t in finding_types.LABELS if finding_types.group_of(t)]
        for prefix in finding_types.GROUPS:
            self.assertTrue(any(t == prefix or t.startswith(prefix) for t in finding_types.LABELS),
                            f"{prefix} names no finding type")
        self.assertGreater(len(named), 20)

    def test_the_findings_page_reads_the_group_from_its_address(self):
        src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
        self.assertIn('new URLSearchParams(location.search).get("group")', src)
        self.assertIn('"type_groups": finding_types.groups_for(', src)

    def test_the_narrowed_entry_is_marked_as_the_current_page_and_findings_gives_it_back(self):
        """OBS-572: on `findings.html?group=security` the Security entry carries aria-current, not Findings;
        the reset and a finding link give it back. The server cannot know the query, so the script marks it."""
        src = (ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("""'#pages a.pg-view[href$="group=' + group + '"]'""", src)
        self.assertEqual(src.count("navMark(true)"), 1)
        self.assertEqual(src.count("navMark(false)"), 2, "the reset button and a #f- link both clear the narrowing")
        nav = shell.nav_html("findings", {})
        self.assertEqual(nav.count('aria-current="page"'), 1, "the built page marks one entry, Findings")
        self.assertIn('class="pg pg-view" href="findings.html?group=security"', nav)


class PageTable(unittest.TestCase):
    """OBS-572: every page list is derived from `dashboard/page_table.py`, so a page is one entry."""

    def test_the_lists_are_derived_from_the_one_table(self):
        names = [p.name for p in shell.PAGE_TABLE]
        self.assertEqual(list(shell.NAMES), names)
        self.assertEqual(len(set(names)), len(names), "a page is declared once")
        self.assertEqual(list(shell.QUESTIONS), names, "every page has its question")
        self.assertEqual([n for n, _t, _k in shell.PAGES], names)
        self.assertEqual(sorted(n for _k, _l, ns in shell.NAV_GROUPS for n in ns if n not in shell.NAV_LINKS),
                         sorted(names), "every page is in one navigation group")

    def test_every_body_kind_names_a_module_that_draws_it_and_a_slot_the_template_carries(self):
        source = ast.parse((ROOT / "dashboard/build_dashboard.py").read_text(encoding="utf-8"))
        template = next(ast.literal_eval(n.value) for n in source.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "TEMPLATE" for t in n.targets))
        self.assertEqual(template.count("__BODY_SLOTS__"), 1, "the slots are written once, from the table")
        kinds = {p.kind for p in shell.PAGE_TABLE}
        for kind, (slot, module, func, _takes) in shell.BODIES.items():
            self.assertIn(kind, kinds, f"{kind} draws a body no page has")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)), f"{module}.{func}")
            self.assertEqual(slot, kind.upper())
        for page in shell.PAGE_TABLE:
            self.assertTrue(page.kind in shell.BODIES or page.kind in ("index", "table", "findings", "health"),
                            f"{page.name}: no body for kind {page.kind}")

    def test_a_page_fills_only_its_own_slot_and_the_others_stay_empty(self):
        slots = "\n".join(f"__{s}__" for s in shell.SLOTS)
        for name, kind in (("machine", "machine"), ("disk", "disk"), ("tools", "tools"), ("agents", "agents")):
            page = shell.page_html("__NAV__\n" + slots, name, {})
            self.assertNotIn("__", page, f"{name}: a slot was left unfilled")
        self.assertEqual(shell.page_html(slots, "projects", {}).strip(), "")

    def test_the_build_output_list_reads_the_same_table(self):
        spec = importlib.util.spec_from_file_location("observatory_cli", ROOT / "observatory.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        self.assertEqual(cli._PAGE_NAMES, shell.NAMES)
        for name in shell.NAMES:
            self.assertIn(f"docs/dashboard/{name}.html", cli.IGNORED_WRITES_ALLOWED)


if __name__ == "__main__":
    unittest.main()
