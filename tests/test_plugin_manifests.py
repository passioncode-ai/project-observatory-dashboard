"""The companion plugin is published from the repository root and shipped in the wheel.

`claude plugin marketplace add passioncode-ai/project-observatory-dashboard` reads the root
manifest; directory installs read the one inside the engine. Both must describe
the same plugin at the same version, and the root one must point at real files.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_SKILL = ROOT / "observatory/engine/skill"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PluginManifestTest(unittest.TestCase):
    def test_root_marketplace_points_at_the_shipped_plugin(self):
        root = load(ROOT / ".claude-plugin/marketplace.json")
        entry = root["plugins"][0]
        self.assertEqual(root["name"], "observatory-log")
        self.assertEqual(entry["name"], "observatory-log")
        plugin_dir = (ROOT / entry["source"]).resolve()
        self.assertEqual(plugin_dir, (ENGINE_SKILL / "plugins/observatory-log").resolve())
        self.assertTrue((plugin_dir / ".claude-plugin/plugin.json").is_file())
        self.assertTrue((plugin_dir / "hooks/hooks.json").is_file())

    def test_every_manifest_and_skill_carries_one_version(self):
        plugin = load(ENGINE_SKILL / "plugins/observatory-log/.claude-plugin/plugin.json")
        versions = {
            "plugin.json": plugin["version"],
            "root marketplace": load(ROOT / ".claude-plugin/marketplace.json")["plugins"][0]["version"],
            "engine marketplace": load(ENGINE_SKILL / ".claude-plugin/marketplace.json")["plugins"][0]["version"],
        }
        for skill in (ENGINE_SKILL / "plugins/observatory-log/skills").glob("*/SKILL.md"):
            for line in skill.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version:"):
                    versions[skill.parent.name] = line.split(":", 1)[1].strip().strip('"')
        self.assertEqual(len(set(versions.values())), 1, versions)

    def test_schema_urls_resolve(self):
        # Measured with curl on 2026-09-29: claude-code-plugin.json answers 404;
        # these two answer 200 (after one redirect to www.schemastore.org) and
        # each schema's own $id is exactly this address.
        plugin = load(ENGINE_SKILL / "plugins/observatory-log/.claude-plugin/plugin.json")
        self.assertEqual(plugin.get("$schema"), "https://json.schemastore.org/claude-code-plugin-manifest.json")
        for path in (ROOT / ".claude-plugin/marketplace.json", ENGINE_SKILL / ".claude-plugin/marketplace.json"):
            self.assertEqual(load(path).get("$schema"), "https://json.schemastore.org/claude-code-marketplace.json",
                             path)

    def test_homepage_is_this_repository(self):
        plugin = load(ENGINE_SKILL / "plugins/observatory-log/.claude-plugin/plugin.json")
        self.assertEqual(plugin["homepage"], "https://github.com/passioncode-ai/project-observatory-dashboard")


if __name__ == "__main__":
    unittest.main()
