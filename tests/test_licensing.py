"""The repository states one license, in every place a reader or a tool looks for it.

From the release after v0.9.1 the code is open source under the GNU Affero General Public
License v3.0 only, or available under a commercial license from PassionCode.ai
(`AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`, the organisation's knowledge base,
knowledge/licensing.md). Earlier releases
keep their license: v0.8.2 to v0.9.1 PolyForm Noncommercial or Internal Use, v0.8.1 and
earlier MIT. A manifest that still names PolyForm, a README that still calls the current
version source-available or a LICENSE that is not the unmodified AGPL text would each tell a
reader something false, so each is a test.
"""
import hashlib
import importlib.util
import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "observatory/engine/skill"
EXPRESSION = "AGPL-3.0-only OR LicenseRef-PassionCode-Commercial"
#: The unmodified AGPL-3.0 text from gnu.org, as the organisation's knowledge base
#: (fabric-workspace knowledge/repository-standard.md, rule F7) pins it.
AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
SHORT_LINE = "Open source under the [GNU AGPL-3.0](LICENSE)."
HISTORY = ("Versions up to and including v0.9.1 were released under PolyForm Noncommercial or "
           "Internal Use (v0.8.2–v0.9.1) and the MIT License (v0.8.1 and earlier); those releases "
           "keep their licence.")
PUBLISHER = {"name": "PassionCode.ai", "url": "https://passioncode.ai/"}


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class LicenseFileTest(unittest.TestCase):
    def test_license_is_the_unmodified_agpl_text(self):
        body = (ROOT / "LICENSE").read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), AGPL_SHA256)
        self.assertTrue(body.startswith(b"                    GNU AFFERO GENERAL PUBLIC LICENSE\n"))

    def test_commercial_license_offers_the_dual_licence(self):
        body = text("COMMERCIAL-LICENSE.md")
        self.assertTrue(body.startswith("# Commercial license\n"))
        self.assertIn(f"SPDX: `{EXPRESSION}`", body)
        self.assertIn("contact@passioncode.ai", body)
        self.assertIn("[CLA.md](CLA.md)", body)


class PackageMetadataTest(unittest.TestCase):
    def setUp(self):
        self.pyproject = tomllib.loads(text("pyproject.toml"))

    def test_pyproject_uses_the_spdx_expression(self):
        project = self.pyproject["project"]
        self.assertEqual(project["license"], EXPRESSION)
        self.assertEqual(project["license-files"], ["LICENSE", "COMMERCIAL-LICENSE.md"])
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
        self.assertTrue(privacy.allowed_path(Path("COMMERCIAL-LICENSE.md")))

    def test_contributing_accepts_contributions_under_the_cla(self):
        contributing = text("CONTRIBUTING.md")
        self.assertIn("[CLA.md](CLA.md)", contributing)
        self.assertIn(EXPRESSION, contributing)
        self.assertIn("](COMMERCIAL-LICENSE.md)", contributing)

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

    def test_no_current_document_calls_the_product_source_available_or_mit(self):
        for path in self.CURRENT:
            body = text(path)
            # "released under PolyForm … (v0.8.2–v0.9.1)" names a past release and is true;
            # a sentence that calls the product source-available now is not.
            self.assertIsNone(re.search(r"(?i)source[- ]available", body), path)
            self.assertIsNone(re.search(r"(?i)PolyForm-Noncommercial|LicenseRef-PolyForm", body), path)
            self.assertIsNone(re.search(r"\bMIT licensed\b|·\s*MIT\b|opensource\.org/license/mit", body), path)

    def test_readme_states_the_license_and_its_history(self):
        readme = text("README.md")
        section = readme.split("\n## License\n", 1)[1]
        self.assertIn(SHORT_LINE, section)
        self.assertIn("[commercial license](COMMERCIAL-LICENSE.md)", section)
        self.assertIn("contact@passioncode.ai", section)
        self.assertIn(HISTORY, section)
        self.assertNotIn("Version 0.2 brings", readme)

    def test_contacts_are_organisational(self):
        for path in ("SECURITY.md", "CODE_OF_CONDUCT.md"):
            body = text(path)
            self.assertIn("contact@passioncode.ai", body, path)
            self.assertIn("security/advisories/new", body, path)
            self.assertNotIn("sshlg.me", body, path)


if __name__ == "__main__":
    unittest.main()
