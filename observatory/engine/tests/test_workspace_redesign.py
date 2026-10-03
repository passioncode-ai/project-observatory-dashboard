"""UI-01/04/05/06: execute built dashboard views against synthetic inventories.

This checks rendered records and action semantics, not browser layout. The shared
renderer supplies its minimal DOM; fixtures never read a personal registry.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import dashboard_fixture


# Reuse the real rendering harness's DOM and browser globals, but return full
# output instead of excerpts so an assertion cannot miss a row beyond the clip.
RUNNER = r'''
import { readFileSync } from "node:fs";
const harness = readFileSync(process.argv[2], "utf8");
const spec = JSON.parse(readFileSync(process.argv[3], "utf8"));
let prefix = harness.slice(harness.indexOf('const file ='), harness.indexOf('let threw = null;'));
prefix = prefix.replace('const file = process.argv[2];', 'const file = ' + JSON.stringify(spec.page) + ';');
// Finding controls ask descendants for stable nodes; the shared harness's
// elements answer with them (and record insertAdjacentHTML) on their own.
const exercise = `
const chips = [...html.matchAll(/<button\\b[^>]*data-f="([^"]+)"[^>]*>([\\s\\S]*?)<\\/button>/g)].map(match => {
  const chip = makeEl("chip-" + match[1]);
  chip.dataset.f = match[1]; chip.textContent = match[2]; chip.attrs = {"aria-pressed": "false"};
  chip.setAttribute = (key, value) => { chip.attrs[key] = String(value); };
  chip.getAttribute = key => chip.attrs[key] ?? null;
  return chip;
});
const selectChips = selector => chips.filter(chip => {
  const exact = /data-f="([^"]+)"/.exec(selector);
  return (!exact || chip.dataset.f === exact[1]) && (!selector.includes('aria-pressed="true"') || chip.getAttribute('aria-pressed') === "true");
});
const originalQuery = document.querySelector.bind(document);
document.querySelectorAll = selector => selector.includes("data-f") ? selectChips(selector) : [];
document.querySelector = selector => selector.includes("data-f") ? selectChips(selector)[0] || null : originalQuery(selector);
let body = scripts.join("\\n;\\n");
body = body.replace(/const PAGE = [^;]+;/, "const PAGE = " + JSON.stringify(spec.view) + ";");
// The reader's language: the harness has no localStorage, so the build locale decides.
if (spec.locale) body = body.replace('document.documentElement.getAttribute("data-build-locale") || "en"', JSON.stringify(spec.locale));
body = body.replace("const RUNTIME =", "Object.assign(D, " + JSON.stringify(spec.data || {}) + ");\\nconst RUNTIME =");
body += "\\n;" + (spec.after || "");
// The record of what was written, so a case's after-code can keep a snapshot.
globals.written = written;
const run = new Function(...Object.keys(globals), "\\\"use strict\\\";\\n" + body);
run(...Object.values(globals));
process.stdout.write(JSON.stringify(written));
`;
const run = new Function('readFileSync', 'spec', prefix + exercise);
run(readFileSync, spec);
'''


class WorkspaceRedesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise RuntimeError("node is required for dashboard behavior checks")
        cls.tmp = tempfile.TemporaryDirectory(prefix="observatory-workspace-ui-")
        cls.base = Path(cls.tmp.name).resolve()
        (cls.base / "fixture").mkdir()
        cls.page = dashboard_fixture.build(cls.base / "fixture")
        cls.runner = cls.base / "run.mjs"
        cls.runner.write_text(RUNNER, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, view, data=None, after="", locale=None):
        spec = self.base / "case.json"
        spec.write_text(json.dumps({"page": str(self.page), "view": view, "locale": locale,
                                    "data": data or {}, "after": after}), encoding="utf-8")
        p = subprocess.run(["node", str(self.runner), str(ROOT / "tests/render_dashboard.mjs"), str(spec)],
                           cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr[-2500:])
        return json.loads(p.stdout)

    def test_projects_and_env_show_records_without_expanding_groups(self):
        for view, row_marker in (("projects", "fixture-a"), ("env", "TEST_LOCAL")):
            with self.subTest(view=view):
                out = self.render(view)["out"]
                self.assertIn(row_marker, out)
                self.assertIsNone(re.search(r'<tbody\b[^>]*class="[^"]*\bfolded\b', out), "default view conceals records")
                self.assertGreater(len(re.findall(r'<tr\b', out)), 1)

    @staticmethod
    def traffic():
        return {"google": {"properties": [
            {"id": "zero", "name": "Measured zero", "account_name": "A", "users_30d": 0,
             "standing": "linked", "project": "project:fixture-a", "report_url": "https://example.invalid/zero"},
            {"id": "unknown", "name": "Unknown metric", "account_name": "B", "users_30d": None,
             "standing": "unclaimed", "error": "not measured", "report_url": "https://example.invalid/unknown"},
            {"id": "busy", "name": "Measured traffic", "account_name": "A", "users_30d": 12,
             "standing": "unclaimed", "report_url": "https://example.invalid/busy"}],
            "totals": {}, "scanned_on": "2026-01-01"}}

    def test_no_users_filter_means_measured_zero_not_unknown(self):
        out = self.render("traffic", self.traffic(), 'active.add("t-quiet"); renderTraffic();')["out"]
        self.assertIn("Measured zero", out)
        self.assertFalse("Unknown metric" in out, "unknown metric matched measured-zero filter")
        self.assertNotIn("Measured traffic", out)

    def test_unknown_unclaimed_total_is_not_reported_as_zero(self):
        data = self.traffic()
        data["google"]["properties"] = [data["google"]["properties"][1]]
        data["google"]["totals"] = {"unclaimed": 1, "users_30d_unclaimed": None,
                                     "users_30d": None, "unknown_properties": 1}
        out = self.render("traffic", data)["out"]
        self.assertIn("1 unowned (audience not measured)", out)
        self.assertNotIn("1 unowned (0", out)

    def test_contradictory_traffic_status_chips_replace_each_other(self):
        out = self.render("traffic", self.traffic(),
                          'document.querySelector(\'[data-f="t-linked"]\').onclick(); '
                          'document.querySelector(\'[data-f="t-unclaimed"]\').onclick();')["out"]
        self.assertIn("Measured traffic", out, "choosing unclaimed must release linked filter")
        self.assertNotIn("Measured zero", out)

    def test_traffic_is_comparable_across_account_boundaries(self):
        data = self.traffic()
        data["google"]["properties"].append({"id": "middle", "name": "Middle traffic", "account_name": "B",
                                            "users_30d": 6, "standing": "unclaimed", "report_url": "https://example.invalid/middle"})
        out = self.render("traffic", data, 'SORT.key="users"; SORT.dir="descending"; renderTraffic();')["out"]
        names = ["Measured traffic", "Middle traffic", "Measured zero", "Unknown metric"]
        indices = [out.index(name) for name in names]
        self.assertEqual(indices, sorted(indices), "global comparison must not restart for each account")

    def test_copy_only_credentials_and_refresh_say_command(self):
        creds = {"creds": {"credentials": [{"id": "credential:fixture", "name": "Fixture credential",
                                            "kind": "llm-api-key", "used_by": []}]}}
        for view, data in (("creds", creds), ("traffic", self.traffic())):
            with self.subTest(view=view):
                out = self.render(view, data)["out"]
                labels = [re.sub(r'<[^>]+>', '', m) for m in
                          re.findall(r'<button\b[^>]*\bdata-copy="[^"]*"[^>]*>(.*?)</button>', out, re.S)]
                self.assertTrue(labels, "fixture must expose at least one copied command")
                self.assertTrue(all("command" in label.lower() for label in labels), labels)
                self.assertNotIn("opened from a file", out,
                                 "copy mode also includes a served read-only dashboard")

    @staticmethod
    def findings(items, silenced=None):
        return {"findings": {"items": items, "counts": {"critical": 0, "warning": len(items), "info": 0},
                             "silenced": silenced or [], "built_at": "2026-01-01T00:00:00Z"}}

    def test_warning_only_overview_keeps_attention_visible(self):
        item = {"id": "warning-fixture", "severity": "warning", "type": "fixture.warning",
                "subject": "project:fixture-a", "title": "Attention fixture", "detail": "Measured issue", "action": "Inspect"}
        out = self.render("index", self.findings([item]))["findings"]
        self.assertIn("Attention fixture", out)
        self.assertFalse('class="flist folded"' in out, "warning-only overview hides the attention row")
        self.assertNotIn("No open findings", out)

    def test_acknowledged_only_findings_retain_reason_and_undo(self):
        item = {"id": "ack-fixture", "title": "Acknowledged fixture",
                "acked": {"why": "Synthetic review reason", "by": "fixture"}}
        out = self.render("findings", self.findings([], [item]))["findings"]
        self.assertIn("Acknowledged fixture", out)
        self.assertIn("Synthetic review reason", out)
        self.assertIn("--undo", out)
        self.assertRegex(out, r'data-(?:cmd|copy)="[^"]*--undo')


    # ── defects found by walking all eleven pages in a browser, 2026-10-02 ──

    def test_finding_types_are_named_and_subjects_link_where_the_builder_says(self):
        items = [
            {"id": "f1", "severity": "warning", "type": "heroku.app_down", "subject": "heroku:1111-aaaa",
             "title": "alpha-app is down", "detail": "d", "action": "a",
             "href": "heroku.html#a-alpha-app", "href_label": "alpha-app"},
            {"id": "f2", "severity": "warning", "type": "example.unnamed", "subject": "credential:gone",
             "title": "Unlinked fixture", "detail": "d", "action": "a"},
            {"id": "f3", "severity": "warning", "type": "heroku.orphan_app", "subject": "estate:heroku-orphans",
             "title": "Orphans fixture", "detail": "d", "action": "a",
             "href": "heroku.html?f=noproject", "href_label": "Keys", "href_page": True}]
        data = self.findings(items)
        data["findings"]["type_labels"] = {"heroku.app_down": "Heroku app down",
                                           "heroku.orphan_app": "Heroku apps without a project"}
        for locale, down, orphans in (("en", "Heroku app down", "Heroku apps without a project"),
                                      ("ru", "Приложение Heroku не работает", "Приложения Heroku без проекта")):
            with self.subTest(locale=locale):
                out = self.render("findings", data, locale=locale)["findings"]
                options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', out)
                self.assertIn(("heroku.app_down", f"{down} · 1"), options)
                self.assertIn(("example.unnamed", "example.unnamed · 1"), options, "an unnamed type keeps its id")
                self.assertIn(f'<span class="ftl" title="heroku.orphan_app">{orphans}</span>', out)
                self.assertIn('href="heroku.html#a-alpha-app"', out)
                self.assertIn("→ alpha-app", out)
                self.assertEqual(out.count('class="plink fsubj"'), 2, "a subject without a row is not a link")
        self.assertIn("→ Keys", self.render("findings", data)["findings"], "a page link's label is a page title")

    def test_every_grouped_table_folds_from_its_heading(self):
        apps = {"heroku": {"apps": [{"name": "alpha-app", "team": "example-org", "state": "running",
                                     "monthly_cost": 7, "region": "eu", "stack": "s"}], "scanned_on": "2026-01-01"}}
        mcp = {"mcp": {"servers": [{"name": "docs", "agent": "example-agent", "scope": "user",
                                    "liveness": "connected", "liveness_detail": "Connected"}],
                       "totals": {"declarations": 1, "distinct_servers": 1}}}
        doms = {"domains": [{"name": "alpha.example.com", "registrar": "example-registrar", "projects": []}]}
        creds = {"creds": {"credentials": [{"id": "credential:fixture", "name": "Fixture", "kind": "llm-api-key",
                                            "used_by": [], "limit": 100, "limit_reset": "monthly", "usage": 1.5}]}}
        for view, data in (("heroku", apps), ("mcp", mcp), ("domains", doms), ("creds", creds),
                           ("traffic", self.traffic()), ("env", {})):
            with self.subTest(view=view):
                out = self.render(view, data)["out"]
                groups = len(re.findall(r'<tbody class="grp', out))
                self.assertGreater(groups, 0)
                self.assertEqual(len(re.findall(r'<button class="grp-fold" type="button" aria-expanded="true">', out)),
                                 groups, "every group heading is its fold control")

    def test_keys_and_mcp_speak_the_readers_language_with_units(self):
        creds = {"creds": {"credentials": [{"id": "credential:fixture", "name": "Fixture", "kind": "llm-api-key",
                                            "used_by": [], "limit": 100, "limit_reset": "monthly", "usage": 1.5}]}}
        out = self.render("creds", creds, locale="ru")["out"]
        self.assertIn("$100", out)
        self.assertIn("ежемесячно", out)
        self.assertNotIn(">monthly<", out)
        self.assertIn("$1.500", out)
        mcp = {"mcp": {"servers": [{"name": "docs", "agent": "example-agent", "scope": "user",
                                    "liveness": "needs-auth", "liveness_detail": "Needs authentication"}],
                       "totals": {}}}
        out = self.render("mcp", mcp, locale="ru")["out"]
        self.assertIn(">нужен вход</div>", out)
        self.assertIn('title="Needs authentication"', out)
        out = self.render("traffic", self.traffic(), locale="ru")["out"]
        self.assertIn("Показано <b>3</b> из 3 ресурсов", out)
        self.assertNotIn("property", out.split("<table")[0].lower())

    def test_counts_after_of_take_the_genitive(self):
        creds = {"creds": {"credentials": [{"id": f"credential:c{i}", "name": f"c{i}", "kind": "machine-secret",
                                            "used_by": []} for i in range(4)]}}
        out = self.render("creds", creds, locale="ru")["out"]
        self.assertIn("Показано <b>4</b> из 4 записей", out)

    def test_env_links_only_known_projects_and_options_only_visible_ones(self):
        env = {"env": {"files": [
            {"path": "alpha-folder/.env", "project": "alpha-folder", "kind": "env", "git": "ignored", "mode": "600",
             "modified_on": "2026-01-01", "variables": [
                 {"name": "SHARED_TOKEN", "class": "secret", "shared_with": ["beta-folder", "gamma-folder"]}]},
            {"path": "beta-folder/.env.example", "project": "beta-folder", "kind": "template", "git": "ignored",
             "mode": "600", "modified_on": "2026-01-01", "variables": [{"name": "ONLY_TEMPLATE", "class": "secret"}]}],
            "totals": {}, "project_ids": {"alpha-folder": "project:alpha-web", "beta-folder": "project:beta-api"}}}
        out = self.render("env", env, 'sel.dataset.filled = ""; fillOwners(); written.optionsDefault = sel.innerHTML;'
                                      'active.add("e-tpl"); fillOwners(); written.optionsWide = sel.innerHTML;')
        links = out["out"]
        self.assertIn('href="#project:beta-api">beta-folder</a>', links)
        self.assertNotIn('#project:gamma-folder', links, "a folder the registry does not know is not a link")
        self.assertRegex(links, r'<span class="unlinked" title="[^"]+">gamma-folder</span>')
        self.assertIn('value="alpha-folder"', out["optionsDefault"])
        self.assertNotIn('value="beta-folder"', out["optionsDefault"], "a project whose rows are all hidden is no option")
        self.assertIn('value="beta-folder"', out["optionsWide"])

    def test_domains_offer_a_dash_for_names_without_a_registrar(self):
        doms = {"domains": [{"name": "alpha.example.com", "registrar": "example-registrar", "projects": []},
                            {"name": "beta.example.com", "registrar": None, "projects": []}],
                "zones": [{"name": "zone.example.com"}]}
        out = self.render("domains", doms, 'fillOwners(); written.options = sel.innerHTML;'
                                         'sel.value = NO_REGISTRAR; renderDomains();')
        self.assertIn('>—</option>', out["options"])
        self.assertIn("beta.example.com", out["out"])
        self.assertIn("zone.example.com", out["out"])
        self.assertNotIn("alpha.example.com", out["out"])

    def test_project_panel_folds_repeats_translates_enums_and_names_a_missing_project(self):
        after = ('const r0 = D.rows[0]; r0.anchor = "vault-folder"; r0.lifecycle = "active"; r0.tier = "cold";'
                 'r0.products = [{name: "Example product", role: "other", kind: "declared"}];'
                 'r0.notes = [{at: "2026-01-09", text: "Same conclusion", state: "observed", conf: 0.5, n: 3}];'
                 'detail(r0.id); written.known = written.panel; detail("project:not-in-registry");')
        out = self.render("projects", {}, after, locale="ru")
        self.assertIn("×3", out["known"])
        self.assertEqual(out["known"].count("Same conclusion"), 1)
        for word in ("папка в хранилище заметок", "активен", "другое", "наблюдение"):
            self.assertIn(word, out["known"])
        for raw in (">vault-folder", "— other<", ">observed"):
            self.assertNotIn(raw, out["known"])
        self.assertIn('id="panel-missing"', out["panel"])
        self.assertIn("not-in-registry", out["panel"])

    def test_tiles_agree_with_their_number_and_drop_the_count_when_open(self):
        after = ('written.rest = tileHTML(TILES_REST);'
                 'const b = document.getElementById("more-tiles"); b.getAttribute = () => "false";'
                 'MORE_TILES.onclick({currentTarget: b}); written.count = b.querySelector("b").textContent;')
        stats = {"stats": {"projects": 3, "inactive_repos": 1, "creds_leaked": 24, "commits_7d": 5}}
        out = self.render("index", stats, after, locale="ru")
        self.assertIn("<b>5</b><span>коммитов за 7 дн</span>", out["work"])
        self.assertIn("<b>1</b><span>неактивный репо</span>", out["rest"])
        self.assertIn("<b>24</b><span>ключа утекли</span>", out["rest"])
        self.assertEqual(out["count"], "−", "an open counter list shows no '+N'")

    def test_health_spend_has_its_unit_and_the_queue_hands_over_the_rest(self):
        data = {"health": {"spend_month": 1.25, "spend_today": 0.0051, "spend_denomination": "credits", "proposed": 30},
                "queue": [{"id": "mem-1", "rev": 1, "kind": "fact", "at": "2026-01-01", "statement": "Fixture"}]}
        out = self.render("health", data)
        self.assertIn("0.0051 credits", out["health"])
        self.assertIn("1.2500 credits", out["health"])
        self.assertIn('id="queue-rest"', out["queue"])
        self.assertIn("review.py&#39; &#39;list", out["queue"])

    def test_the_copy_toast_names_the_end_of_the_command(self):
        out = self.render("index", {}, 'toast(T("copied: {what}", {what: copiedWhat("Command: silence", '
                                       '"/very/long/interpreter/path/python3 /very/long/engine/path/tools/ack.py finding:1 --why x")}));')
        self.assertIn("Command: silence", out["toast"])
        self.assertIn("tools/ack.py finding:1 --why x", out["toast"])
        self.assertNotIn("/very/long/interpreter", out["toast"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
