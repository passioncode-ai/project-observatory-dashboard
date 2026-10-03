"""The dashboard in two languages: English by default, Russian by choice.

What this proves, offline:

* the dashboard's sources carry no Russian text — every reader-facing string is
  an English message id, and the Russian lives in `dashboard/locales/ru.json`;
* every message id the code names has a Russian translation, with the same
  placeholders, and every plural carries the forms its language needs;
* the plural rules the builder uses are the ones the browser uses
  (`Intl.PluralRules`, run in Node when it is installed);
* the workspace setting `interface.locale` accepts `en`/`ru` only, defaults to
  English, and is written by `configure interface locale`;
* pages built in each language say so in `lang`, carry the reader's switch, and
  an English page holds no Russian except the switch's own name for Russian;
* the PassionCode design tokens are the vendored bytes the manifest pins.
"""
from __future__ import annotations

import ast
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DASH))
sys.path.insert(0, str(ROOT / "tests"))
import i18n  # noqa: E402
import tmp  # noqa: E402
import shell  # noqa: E402

CYRILLIC = re.compile(r"[Ѐ-ӿ]")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
PLURAL_FORMS = {"ru": {"one", "few", "many", "other"}, "en": {"one", "other"}}
#: Ids the code builds at run time rather than naming as a literal: search
#: placeholders chosen by tab, table units passed to `filterLine`, plugin
#: labels read from manifests, and the store's degradation messages.
DYNAMIC_PATTERNS = (
    re.compile(r'filterLine\([^;]*?"((?:of@@)?\{n\}[^"]*)"'),
    # Tile captions: `tile("…", value)` and the work tiles' `tile@@` plurals.
    re.compile(r'\btile\(\s*"([^"]+)"'),
    re.compile(r'"(tile@@\{n\}[^"]*)"'),
    re.compile(r'(?:projects|heroku|domains|creds|env|mcp|traffic):\s*"(Search[^"]*)"'),
    re.compile(r'"text":\s*"([^"]+)"'),
    re.compile(r'"label":\s*"([^"]+)"'),
)


def _python_ids(path: Path) -> tuple[set[str], str]:
    """Message ids in Python calls — `t("…")`, `T(…)`, `.mark("…")`,
    `.attr(name, "…")` — read from the syntax tree, so an implicitly joined
    multi-line literal is one id; and the TEMPLATE literal, for the script."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ids, template = set(), ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "TEMPLATE" for t in node.targets) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            template = node.value.value
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        index = {"t": 0, "T": 0, "translate": 0, "mark": 0, "attr": 1}.get(name)
        if index is None or len(node.args) <= index:
            continue
        arg = node.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            ids.add(arg.value)
    return ids, template


def machine_page_ids() -> set[str]:
    """The Machine page's ids: `t`/`mark` literals, every literal passed to
    `_table` (caption, column heads, empty text) and the values it translates
    through a variable (`machine_page.DYNAMIC`)."""
    tree = ast.parse((DASH / "machine_page.py").read_text(encoding="utf-8"))
    ids, _ = _python_ids(DASH / "machine_page.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_table":
            for arg in node.args[1:]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    ids.add(arg.value)
                elif isinstance(arg, ast.List):
                    ids |= {e.elts[0].value for e in arg.elts
                            if isinstance(e, ast.Tuple) and isinstance(e.elts[0], ast.Constant)}
    sys.path.insert(0, str(DASH))
    import machine_page
    return ids | set(machine_page.DYNAMIC)


def source_ids() -> set[str]:
    """Every message id named in the dashboard's code and static markup."""
    ids, template = _python_ids(DASH / "build_dashboard.py")
    shell_ids, _ = _python_ids(DASH / "shell.py")
    ids |= shell_ids
    ids |= machine_page_ids()
    literal = r'(["\'])((?:\\.|(?!\1).)+?)\1'
    for m in re.finditer(r"\bT\(\s*" + literal, template):
        ids.add(m.group(2).replace('\\"', '"').replace("\\'", "'"))
    for m in re.finditer(r"<[a-z0-9]+\b[^>]*\sdata-t>(.*?)</", template, re.S):
        ids.add(" ".join(m.group(1).split()))
    # Static attributes of the markup (not of markup the script builds).
    static = template[template.index("<body>"):template.index("<script>", template.index("<body>"))]
    for m in re.finditer(r'\s(aria-label|placeholder|title)="([^"]*)"', static):
        if any(c.isalpha() for c in m.group(2)) and not m.group(2).startswith("__"):
            ids.add(html.unescape(m.group(2)))
    for pattern in DYNAMIC_PATTERNS:
        for m in pattern.finditer(template + (DASH / "build_dashboard.py").read_text(encoding="utf-8")):
            ids.add(m.group(1))
    # shell.py: the page titles, their questions and the navigation groups.
    ids |= {title for _n, title, _k in shell.PAGES} | set(shell.QUESTIONS.values())
    ids |= {label for _k, label, _names in shell.NAV_GROUPS}
    # finding_types.py: the reader's name for every finding type.
    import finding_types
    ids |= set(finding_types.LABELS.values())
    for manifest in (ROOT / "plugins").glob("*.json"):
        for metric in json.loads(manifest.read_text(encoding="utf-8")).get("metrics", []):
            if metric.get("label"):
                ids.add(metric["label"])
    ids |= DYNAMIC_IDS
    # The rule modules' finding titles: each `titled("…")` id and each clone
    # state's title id, translated like any other string the page shows.
    ids |= {msgid for _module, msgid, _args, _line in finding_title_calls()}
    # Ids that are not reader-facing text: technical names and the product's.
    return {i for i in ids if any(c.isalpha() for c in i) and i not in UNTRANSLATED}


#: The rule modules that build findings: every `tools/*findings*.py`.
FINDING_PRODUCERS = sorted((ROOT / "tools").glob("*findings*.py"))


def finding_title_calls() -> list[tuple[str, str, set[str] | None, int]]:
    """(module, title id, argument names, line) for every finding title.

    Read from the syntax tree: each `titled("<id>", name=…)` call whose id is a
    literal, and each clone state's title id in `build_findings.SYNC_FINDINGS`
    (the second element of its row, filled with `repo` alone). Argument names
    are None where the call passes `**mapping` and so cannot be named here."""
    out = []
    for module in FINDING_PRODUCERS:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "titled" and node.args \
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = None if any(k.arg is None for k in node.keywords) else {k.arg for k in node.keywords}
                out.append((module.name, node.args[0].value, names, node.lineno))
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SYNC_FINDINGS" for t in node.targets):
                for state, row in ast.literal_eval(node.value).items():
                    out.append((module.name, row[1], {"repo"}, node.lineno))
    return out


def _titled(node: ast.AST) -> bool:
    """`titled(…)`, or a conditional whose every branch is one."""
    if isinstance(node, ast.IfExp):
        return _titled(node.body) and _titled(node.orelse)
    return isinstance(node, ast.Call) and getattr(node.func, "id", "") == "titled"


def untitled_finding_literals() -> list[str]:
    """`module:line` of every dict literal in a rule module that carries a
    finding title without its message id.

    Two shapes are refused: a `"title"` key with no `"title_id"` beside it
    (unless the value is copied from another dict's `["title"]`, as the
    notifier does), and a finding-shaped literal — `"type"` and `"severity"`
    keys — whose title does not come from `**titled(…)`."""
    bad = []
    for module in FINDING_PRODUCERS:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            spread = any(k is None and _titled(v) for k, v in zip(node.keys, node.values))
            copied = any(isinstance(k, ast.Constant) and k.value == "title" and isinstance(v, ast.Subscript)
                         and isinstance(v.slice, ast.Constant) and v.slice.value == "title"
                         for k, v in zip(node.keys, node.values))
            if "title" in keys and "title_id" not in keys and not copied:
                bad.append(f"{module.name}:{node.lineno}")
            elif {"type", "severity"} <= keys and not spread and "title_id" not in keys:
                bad.append(f"{module.name}:{node.lineno}")
    return bad


#: Ids reached through a variable, each named with where it is used.
DYNAMIC_IDS = {
    "Projects — the operator's registry",  # build(): the single page's title, via `title`
    "Search",                              # render(): the placeholder when no tab matches
    "nothing to lose",                     # at_risk_total(): a stats value the script translates
}


#: Words that read the same in both languages and are never translated.
UNTRANSLATED = {"Heroku", "ENV", "MCP", "Cloudflare", "GA4", "RDAP",
                "Project Observatory", "PassionCode.ai", "English", "Русский", "__TITLE__"}


class SourcesCarryNoRussian(unittest.TestCase):
    def test_dashboard_sources(self):
        for name in ("build_dashboard.py", "shell.py", "workspace.css", "i18n.py",
                     "finding_types.py", "subject_links.py", "machine_page.py"):
            text = (DASH / name).read_text(encoding="utf-8")
            if name == "i18n.py":
                text = text.replace('"ru": "Русский"', "")
            found = [line for line in text.splitlines() if CYRILLIC.search(line)]
            self.assertEqual(found, [], f"{name} carries Russian text: {found[:3]}")

    def test_plugin_labels_are_english(self):
        for manifest in (ROOT / "plugins").glob("*.json"):
            self.assertIsNone(CYRILLIC.search(manifest.read_text(encoding="utf-8")), manifest.name)


class CatalogIsComplete(unittest.TestCase):
    def test_every_source_id_has_a_russian_translation(self):
        ru = i18n.catalog("ru")
        missing = sorted(source_ids() - set(ru))
        self.assertEqual(missing, [], f"{len(missing)} message id(s) without Russian: {missing[:8]}")

    def test_no_translation_is_left_behind(self):
        # A catalog entry no source names is a string the reader can never see:
        # usually a renamed id whose old translation was not removed.
        ru = set(i18n.catalog("ru"))
        extra = sorted(ru - source_ids() - SESSION_HOOK_IDS)
        self.assertEqual(extra, [], f"unused translations: {extra[:8]}")

    def test_placeholders_match(self):
        for locale in i18n.LOCALES:
            for msgid, entry in i18n.catalog(locale).items():
                want = set(PLACEHOLDER.findall(i18n.english(msgid)))
                forms = entry.values() if isinstance(entry, dict) else [entry]
                for form in forms:
                    self.assertIsInstance(form, str, f"{locale}: {msgid!r}")
                    self.assertEqual(set(PLACEHOLDER.findall(form)), want, f"{locale}: {msgid!r} → {form!r}")
                    self.assertTrue(form.strip(), f"{locale}: empty translation for {msgid!r}")

    def test_russian_keeps_one_vocabulary(self):
        # English words and slang left in the Russian ("по имени property",
        # "в workspace", "эстейт", "claim'ит", "кред", "дэшборд") read as an
        # unfinished translation; each has a Russian term the rest of the
        # catalog already uses (ресурс, папка данных, наши проекты, заявлять,
        # ключ, дашборд). Placeholders are not text and are skipped.
        banned = ("property", "workspace", "эстейт", "claim", " кред", "дэшборд", "стэш")
        for msgid, entry in i18n.catalog("ru").items():
            for form in (entry.values() if isinstance(entry, dict) else [entry]):
                text = PLACEHOLDER.sub("", form).lower()
                hits = [w for w in banned if w in text]
                self.assertEqual(hits, [], f"{msgid!r} → {form!r}")

    def test_plural_forms_are_complete(self):
        for locale, forms in PLURAL_FORMS.items():
            for msgid, entry in i18n.catalog(locale).items():
                if isinstance(entry, dict):
                    self.assertEqual(set(entry), forms, f"{locale}: {msgid!r}")
                    self.assertIn("{n}", i18n.english(msgid), f"{locale}: plural without n: {msgid!r}")

    def test_russian_plural_ids_have_an_english_plural(self):
        ru, en = i18n.catalog("ru"), i18n.catalog("en")
        for msgid, entry in ru.items():
            if isinstance(entry, dict) and "{n}" in msgid:
                self.assertIsInstance(en.get(msgid), dict, f"English has no plural for {msgid!r}")

    def test_english_catalog_holds_only_plurals_and_contexts(self):
        for msgid, entry in i18n.catalog("en").items():
            self.assertTrue(isinstance(entry, dict) or i18n.CONTEXT_SEPARATOR in msgid, msgid)


#: The session-start hook's line, which is not part of the dashboard source.
SESSION_HOOK_IDS = {
    "{crit} critical / {warn} warning", "no findings", "keys by name: {total} (registry {vault}, .env {env})",
    "activity {date}",
    "folder {name} is not in the registry — the next tick picks it up (if the schedule is on), or now: project-observatory full local",
    "{path} is outside the configured projects folder — the observatory does not scan it; recorded in sessions-seen.jsonl. To observe it, move it into the projects folder or change sources.projects",
    # organization_line(): whose accounts the project uses, and the duty to report resources.
    "organization unresolved: {why} — ask the operator before creating accounts",
    "organization: external — not the estate's accounts; create nothing on its behalf",
    "GA {account}", "Figma {team}", "organization: {name}",
    "resources recorded: {n}. Anything you create for it — analytics, Firebase/Google Cloud, servers, cloud or payment accounts, Figma files — is reported with observatory_propose {\"resources\": [...]} in the same turn",
}


class Translation(unittest.TestCase):
    def test_english_is_the_default_and_fallback(self):
        self.assertEqual(i18n.DEFAULT_LOCALE, "en")
        self.assertEqual(i18n.translate("Findings"), "Findings")
        self.assertEqual(i18n.translate("Findings", "ru"), "Находки")
        self.assertEqual(i18n.translate("an id nobody translated", "ru"), "an id nobody translated")
        self.assertEqual(i18n.translate("domain@@not measured"), "not measured")
        self.assertEqual(i18n.translate("domain@@not measured", "ru"), "не измерен")

    def test_plurals_and_numbers(self):
        cases = {1: "1 проект", 2: "2 проекта", 5: "5 проектов", 11: "11 проектов", 21: "21 проект", 1200: "1 200 проектов"}
        for n, want in cases.items():
            self.assertEqual(i18n.translate("{n} projects", "ru", n=n), want)
        self.assertEqual(i18n.translate("{n} projects", "en", n=1), "1 project")
        self.assertEqual(i18n.translate("{n} projects", "en", n=1200), "1,200 projects")

    def test_unknown_locale_is_refused(self):
        with self.assertRaises(i18n.LocaleError):
            i18n.Translator("de")

    def test_python_plural_rules_match_the_browser(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; Intl.PluralRules cannot be compared")
        script = ("const out={};for(const l of ['en','ru']){const r=new Intl.PluralRules(l);"
                  "out[l]=[...Array(250).keys()].map(n=>r.select(n));}console.log(JSON.stringify(out))")
        browser = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
        for locale in i18n.LOCALES:
            ours = [i18n.plural_category(locale, n) for n in range(250)]
            self.assertEqual(ours, browser[locale], locale)


class FindingTitles(unittest.TestCase):
    """A finding's title is a message id and its arguments, so the board can
    say it in the reader's language; `title` stays the English rendering of the
    same pair for every reader that is not the page (MCP, notifications)."""

    def test_every_finding_title_carries_a_message_id(self):
        calls = finding_title_calls()
        self.assertGreater(len(calls), 100, "the rule modules were not read")
        bad = untitled_finding_literals()
        self.assertEqual(bad, [], f"{len(bad)} finding title(s) without a title_id: {bad[:10]}")

    def test_every_finding_title_id_is_in_both_catalogs(self):
        en, ru = i18n.catalog("en"), i18n.catalog("ru")
        for module, msgid, names, line in finding_title_calls():
            where = f"{module}:{line} {msgid!r}"
            with self.subTest(where=where):
                self.assertTrue(msgid in ru, f"no Russian for {where}")
                want = set(PLACEHOLDER.findall(i18n.english(msgid)))
                if names is not None:
                    self.assertEqual(names, want, f"arguments do not match the placeholders: {where}")
                if "{n}" in msgid:
                    # A counted title agrees with its number in both languages.
                    self.assertIsInstance(en.get(msgid), dict, f"no English plural for {where}")
                    self.assertIsInstance(ru.get(msgid), dict, f"no Russian plural for {where}")
                else:
                    # English needs no entry: the id is its English text.
                    self.assertNotIn(msgid, en, f"{where}: English holds only plurals and contexts")

    def test_title_is_the_english_rendering_of_its_id(self):
        import finding_types
        f = finding_types.titled("{host} expires in {days} days", host="alpha.example.com", days=12)
        self.assertEqual(f, {"title": "alpha.example.com expires in 12 days",
                             "title_id": "{host} expires in {days} days",
                             "title_args": {"host": "alpha.example.com", "days": 12}})
        self.assertEqual(i18n.translate(f["title_id"], "ru", **f["title_args"]),
                         "alpha.example.com: домен истекает через 12 дн.")
        # A counted title takes its number's form, in English and in Russian.
        one = finding_types.titled("{n} MCP servers Claude could not reach", n=1)
        many = finding_types.titled("{n} MCP servers Claude could not reach", n=3)
        self.assertEqual(one["title"], "1 MCP server Claude could not reach")
        self.assertEqual(many["title"], "3 MCP servers Claude could not reach")
        for n, word in ((1, "MCP-сервер"), (2, "MCP-сервера"), (5, "MCP-серверов"), (21, "MCP-сервер")):
            self.assertIn(f"{n} {word}", i18n.translate(one["title_id"], "ru", n=n))
        # Arguments are kept JSON-plain: integers stay numbers, the rest text.
        odd = finding_types.titled("the machine survey is {hours} hours old", hours=2.0)
        self.assertEqual(odd["title_args"], {"hours": "2.0"})
        self.assertEqual(odd["title"], "the machine survey is 2.0 hours old")

    def test_rules_render_their_titles_from_the_id(self):
        # Real rules over synthetic input: every title equals the English
        # rendering of its own id and arguments, so the two cannot drift.
        sys.path.insert(0, str(ROOT / "tools"))
        import env_findings
        import heroku_findings
        secret = [{"name": "API_TOKEN", "class": "secret"}]
        rows = env_findings.findings({"files": [
            {"id": "env:alpha-web/.env", "path": "alpha-web/.env", "kind": "env", "git": "tracked",
             "mode": "0644", "variables": secret},
            {"id": "env:beta-api/.env", "path": "beta-api/.env", "kind": "env", "git": "loose",
             "mode": "0644", "variables": secret},
            {"id": "env:gamma/.env", "path": "gamma/.env", "kind": "env", "git": "loose",
             "mode": "0600", "variables": secret}]})
        rows += heroku_findings.app_down([
            {"id": "heroku:1", "name": "alpha-app", "state": "down", "crashed": ["web.1"],
             "monthly_cost": 7.0}])
        self.assertTrue(rows, "the synthetic input produced no finding")
        for f in rows:
            with self.subTest(type=f["type"]):
                self.assertIn("title_id", f)
                self.assertEqual(f["title"], i18n.translate(f["title_id"], "en", **f["title_args"]))
                json.dumps(f["title_args"])  # plain JSON, nothing richer


class StaticMarkup(unittest.TestCase):
    def test_marks_are_filled_and_kept(self):
        out = i18n.localize_markup('<body><button data-t>no project</button>'
                                   '<input placeholder="Search the registry"></body>', "ru")
        self.assertIn('<button data-t="no project">нет проекта</button>', out)
        self.assertIn('placeholder="Поиск по реестру" data-t-placeholder="Search the registry"', out)

    def test_markup_inside_a_message_is_refused(self):
        with self.assertRaises(i18n.LocaleError):
            i18n.localize_markup("<body><p data-t>a <b>bold</b> id</p></body>", "en")

    def test_scripts_are_not_rewritten(self):
        out = i18n.localize_markup('<body><script>x.title = "Findings";</script></body>', "ru")
        self.assertIn('x.title = "Findings";', out)


class WorkspaceSetting(unittest.TestCase):
    def setUp(self):
        self.home = Path(tmp.mkdtemp(prefix="observatory-locale-")).resolve()
        self.env = {**os.environ, "OBSERVATORY_HOME": str(self.home)}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_workspace(self, *argv):
        return subprocess.run([sys.executable, str(ROOT / "observatory.py"), *argv], env=self.env,
                              capture_output=True, text=True)

    def settings(self):
        return json.loads((self.home / "config/settings.json").read_text())

    def test_default_configure_and_refusals(self):
        self.assertEqual(self.run_workspace("init").returncode, 0)
        import configuration
        self.assertEqual(configuration.interface_locale(self.home), "en")
        done = self.run_workspace("configure", "interface", "locale", "ru")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.settings()["interface"], {"locale": "ru"})
        self.assertEqual(configuration.interface_locale(self.home), "ru")
        self.assertEqual(json.loads(self.run_workspace("doctor").stdout)["interface"], {"locale": "ru"})
        for argv, needle in ((("locale", "de"), "must be one of"), (("theme", "dark"), "Unknown interface setting")):
            refused = self.run_workspace("configure", "interface", *argv)
            self.assertEqual(refused.returncode, 2)
            self.assertIn(needle, refused.stderr)
        self.assertEqual(self.settings()["interface"], {"locale": "ru"}, "a refusal must not write")

    def test_a_hand_edited_invalid_value_is_refused_on_load(self):
        self.assertEqual(self.run_workspace("init").returncode, 0)
        import configuration
        doc = self.settings()
        for bad in ({"locale": "fr"}, {"colour": "gold"}, "ru"):
            doc["interface"] = bad
            (self.home / "config/settings.json").write_text(json.dumps(doc))
            with self.assertRaises(configuration.ConfigurationError):
                configuration.load(self.home)


class BuiltPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dashboard_fixture
        cls.dirs = {}
        for locale in i18n.LOCALES:
            root = Path(tmp.mkdtemp(prefix=f"observatory-{locale}-")).resolve()
            old = os.environ.get("OBSERVATORY_LOCALE")
            os.environ["OBSERVATORY_LOCALE"] = locale
            try:
                dashboard_fixture.build(root)
            finally:
                if old is None:
                    os.environ.pop("OBSERVATORY_LOCALE", None)
                else:
                    os.environ["OBSERVATORY_LOCALE"] = old
            cls.dirs[locale] = root / "pages"

    @classmethod
    def tearDownClass(cls):
        for d in cls.dirs.values():
            shutil.rmtree(d.parent, ignore_errors=True)

    def page(self, locale, name="index"):
        return (self.dirs[locale] / f"{name}.html").read_text(encoding="utf-8")

    def test_lang_and_switch(self):
        for locale in i18n.LOCALES:
            text = self.page(locale)
            self.assertIn(f'<html lang="{locale}" data-theme="dark" data-build-locale="{locale}">', text)
            self.assertEqual(text.count('data-locale="'), len(i18n.LOCALES))
            self.assertIn('class="locale-switch"', text)

    def test_english_pages_hold_no_russian(self):
        for name, _title, _kind in shell.PAGES:
            text = self.page("en", name).replace('title="Русский"', "")
            self.assertIsNone(CYRILLIC.search(text), f"{name}.html: {CYRILLIC.search(text) and text[CYRILLIC.search(text).start()-60:][:120]!r}")

    def test_russian_pages_are_russian(self):
        text = self.page("ru")
        for words in ("<title>Обзор — Project Observatory</title>", ">Обзор</h1>", ">Находки<", "Часть набора инструментов PassionCode"):
            self.assertIn(words, text)

    def test_the_script_carries_both_catalogs(self):
        js = (self.dirs["en"] / shell.ASSET_JS).read_text(encoding="utf-8")
        self.assertIn('"Findings": "Находки"', js)
        self.assertNotIn("__I18N__", js)

    def test_brand(self):
        text = self.page("en")
        self.assertIn('class="brand-mark" src="data:image/svg+xml;base64,', text)
        self.assertIn("https://passioncode.ai/", text)
        css = (self.dirs["en"] / shell.ASSET_CSS).read_text(encoding="utf-8")
        self.assertIn("--pc-accent: #ffd21a;", css)
        self.assertIn("--accent: var(--pc-accent);", css)


class VendoredDesignSystem(unittest.TestCase):
    def test_manifest_pins_the_vendored_bytes(self):
        manifest = json.loads((DASH / "brand/manifest.json").read_text())
        self.assertEqual(manifest["system"], "PassionCode")
        for f in manifest["files"]:
            data = (DASH / "brand" / f["vendored"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"], f["vendored"])


if __name__ == "__main__":
    unittest.main()
