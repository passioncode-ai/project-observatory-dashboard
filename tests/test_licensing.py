"""The repository states one license, in every place a reader or a tool looks for it.

From v0.8.2 the code is source-available: PolyForm Noncommercial 1.0.0 or PolyForm
Internal Use 1.0.0, at the user's option, with commercial licenses on request.
Releases up to and including v0.8.1 were published under MIT and stay MIT. A
manifest that still says MIT, a README that still says "open source" or a LICENSE
that lost that sentence would each tell a reader something false, so each is a test.
"""
import importlib.util
import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "observatory/engine/skill"
EXPRESSION = "PolyForm-Noncommercial-1.0.0 OR LicenseRef-PolyForm-Internal-Use-1.0.0"
MIT_HISTORY = ("Versions up to and including v0.8.1 of this repository were released under the MIT "
               "License; those releases remain available under MIT.")
SHORT_LINE = "Source-available under PolyForm Noncommercial or Internal Use; commercial license on request."
PUBLISHER = {"name": "PassionCode.ai", "url": "https://passioncode.ai/"}


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class LicenseFileTest(unittest.TestCase):
    def test_license_carries_both_polyform_texts_and_the_mit_history(self):
        body = text("LICENSE")
        self.assertNotIn("__MIT_HISTORY__", body)
        self.assertIn(MIT_HISTORY, body)
        self.assertIn("Copyright (c) 2026 Siarhei Sheleh", body)
        self.assertIn("Required Notice: Copyright (c) 2026 Siarhei Sheleh (https://passioncode.ai)", body)
        for title, url in (("# PolyForm Noncommercial License 1.0.0",
                            "<https://polyformproject.org/licenses/noncommercial/1.0.0>"),
                           ("# PolyForm Internal Use License 1.0.0",
                            "<https://polyformproject.org/licenses/internal-use/1.0.0>")):
            self.assertIn(title + "\n\n" + url, body)
        self.assertIn("contact@passioncode.ai", body)
        self.assertNotIn("Permission is hereby granted, free of charge", body)


class PackageMetadataTest(unittest.TestCase):
    def setUp(self):
        self.pyproject = tomllib.loads(text("pyproject.toml"))

    def test_pyproject_uses_the_spdx_expression(self):
        project = self.pyproject["project"]
        self.assertEqual(project["license"], EXPRESSION)
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertNotIn("License :: OSI Approved", " ".join(project.get("classifiers", [])))

    def test_build_backend_understands_license_expressions(self):
        # PEP 639 `license = "<expression>"` needs setuptools 77 or newer.
        requires = " ".join(self.pyproject["build-system"]["requires"])
        match = re.search(r"setuptools>=(\d+)", requires)
        self.assertTrue(match and int(match.group(1)) >= 77, requires)

    def test_publisher_and_homepage_are_passioncode(self):
        project = self.pyproject["project"]
        self.assertEqual(project["urls"]["Homepage"], "https://passioncode.ai/observatory/")
        self.assertEqual(project["authors"], [{"name": "PassionCode.ai", "email": "contact@passioncode.ai"}])


class PluginLicenseTest(unittest.TestCase):
    def manifests(self):
        yield "plugin.json", load(SKILL / "plugins/observatory-log/.claude-plugin/plugin.json")
        for label, path in (("root marketplace", ROOT / ".claude-plugin/marketplace.json"),
                            ("engine marketplace", SKILL / ".claude-plugin/marketplace.json")):
            yield label, load(path)["plugins"][0]

    def test_every_manifest_and_skill_states_the_expression(self):
        for label, manifest in self.manifests():
            self.assertEqual(manifest["license"], EXPRESSION, label)
        skills = sorted((SKILL / "plugins/observatory-log/skills").glob("*/SKILL.md"))
        self.assertEqual(len(skills), 3)
        for skill in skills:
            front = skill.read_text(encoding="utf-8").split("\n---", 1)[0]
            self.assertIn(f"license: {EXPRESSION}\n", front, skill.parent.name)

    def test_author_and_owner_are_the_organisation(self):
        for label, manifest in self.manifests():
            self.assertEqual(manifest["author"], PUBLISHER, label)
        for path in (ROOT / ".claude-plugin/marketplace.json", SKILL / ".claude-plugin/marketplace.json"):
            self.assertEqual(load(path)["owner"], PUBLISHER, path)


class ContributionTermsTest(unittest.TestCase):
    def test_cla_is_published_and_admitted_by_the_privacy_gate(self):
        cla = text("CLA.md")
        self.assertTrue(cla.startswith("# Contributor License Agreement\n"))
        self.assertIn("contact@passioncode.ai", cla)
        spec = importlib.util.spec_from_file_location("check_public_release", ROOT / "tools/check_public_release.py")
        privacy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(privacy)
        self.assertTrue(privacy.allowed_path(Path("CLA.md")))

    def test_contributing_accepts_contributions_under_the_cla(self):
        contributing = text("CONTRIBUTING.md")
        self.assertIn("[CLA.md](CLA.md)", contributing)
        self.assertIn(SHORT_LINE, contributing)

    def test_pull_request_template_asks_for_the_cla(self):
        template = text(".github/PULL_REQUEST_TEMPLATE.md")
        self.assertIn("- [ ] I agree to [CLA.md](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/CLA.md)", template)
        # The existing checks stay: the CLA box is added, nothing is replaced.
        self.assertIn("python tools/check_public_release.py --history", template)


class CurrentWordingTest(unittest.TestCase):
    """Documents that describe the product as it is now. Dated receipts under
    docs/runs/, docs/releases/, docs/seo/ and the published posts in docs/content/
    record what was true when they were written and are left as they are."""

    CURRENT = ("README.md", "CONTRIBUTING.md", "AGENTS.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
               "docs/PORTABLE-0.1.md", "docs/HANDOFF.md", "docs/site/DEPLOY.md",
               "site/index.html", "site/404.html", "site/llms.txt")

    def test_no_current_document_calls_the_product_open_source_or_mit(self):
        for path in self.CURRENT:
            body = text(path)
            # "source-available, not open source" is an accurate use of the phrase, and
            # `project-observatory-open-source` is the repository's former name.
            self.assertIsNone(re.search(r"(?i)(?<!not )(?<!observatory-)open[- ]source", body), path)
            self.assertIsNone(re.search(r"\bMIT licensed\b|·\s*MIT\b|opensource\.org/license/mit", body), path)

    def test_readme_states_the_license_and_the_mit_history(self):
        readme = text("README.md")
        self.assertIn(SHORT_LINE, readme)
        self.assertIn("v0.8.1", readme)
        self.assertNotIn("Version 0.2 brings", readme)

    def test_contacts_are_organisational(self):
        for path in ("SECURITY.md", "CODE_OF_CONDUCT.md"):
            body = text(path)
            self.assertIn("contact@passioncode.ai", body, path)
            self.assertIn("security/advisories/new", body, path)
            self.assertNotIn("sshlg.me", body, path)


if __name__ == "__main__":
    unittest.main()
