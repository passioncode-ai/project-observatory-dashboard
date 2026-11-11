"""Add only, improve only (operator rule, 2026-11-11): the engine never stops serving a tool or a capability it
shipped, never drops a parameter a caller may send and never requires a new one. `tools/check_add_only.py` is the
gate; these tests run it on the repository and on synthetic surfaces that break each promise."""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_add_only", ROOT / "tools" / "check_add_only.py")
add_only = importlib.util.module_from_spec(spec)
spec.loader.exec_module(add_only)

SOURCE = '''
@server.tool()
def alpha_read(project: str, limit: int = 20, ctx: "Context | None" = None) -> dict:
    return {}

@server.tool(name="alpha_write", annotations=None)
async def _write(body: dict, *, key: str, note: str | None = None) -> dict:
    return {}

def not_a_tool(x):
    return x
'''
MANIFEST = {"capabilities": [{"name": "alpha.survey", "effect": "none"}, {"name": "alpha.record", "effect": "draft"}]}


def surface(source: str = SOURCE, manifest: dict = MANIFEST) -> dict:
    return {"tools": add_only.tools_of(source), "capabilities": add_only.capabilities_of(manifest)}


def codes(found: list) -> list:
    return sorted((f["code"], f["name"]) for f in found)


class Surface(unittest.TestCase):
    def test_reads_tools_parameters_and_requirements(self) -> None:
        tools = add_only.tools_of(SOURCE)
        self.assertEqual(sorted(tools), ["alpha_read", "alpha_write"])
        self.assertEqual(tools["alpha_read"], {"input": ["limit", "project"], "required": ["project"]})
        self.assertEqual(tools["alpha_write"], {"input": ["body", "key", "note"], "required": ["body", "key"]})


class Promises(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = add_only.locked(surface(), {})

    def test_unchanged_surface_passes(self) -> None:
        self.assertEqual(add_only.check(surface(), self.lock, self.lock), [])

    def test_added_tool_and_optional_parameter_pass_once_locked(self) -> None:
        grown = SOURCE.replace("limit: int = 20,", "limit: int = 20, cursor: str | None = None,") + \
            '\n@server.tool()\ndef alpha_new(x: int = 1) -> dict:\n    return {}\n'
        self.assertEqual(codes(add_only.check(surface(grown), self.lock)), [("unlocked", "alpha_new")])
        relocked = add_only.locked(surface(grown), self.lock)
        self.assertEqual(add_only.check(surface(grown), relocked, self.lock), [])
        self.assertIn("cursor", relocked["tools"]["alpha_read"]["input"])

    def test_removed_tool_is_refused_until_retired(self) -> None:
        shrunk = SOURCE.split("@server.tool(name=")[0]
        self.assertEqual(codes(add_only.check(surface(shrunk), self.lock)), [("missing", "alpha_write")])
        retired = copy.deepcopy(self.lock)
        retired["retired"]["tools"]["alpha_write"] = {"date": "2026-11-11", "reason": "replaced by alpha_write_v2"}
        self.assertEqual(add_only.check(surface(shrunk), retired, self.lock), [])
        retired["retired"]["tools"]["alpha_write"] = {"date": "soon", "reason": ""}
        self.assertEqual(codes(add_only.check(surface(shrunk), retired)), [("retired-incomplete", "alpha_write")])

    def test_dropped_parameter_and_new_requirement_are_refused(self) -> None:
        narrowed = SOURCE.replace("limit: int = 20, ", "").replace("note: str | None = None", "note: str")
        self.assertEqual(codes(add_only.check(surface(narrowed), self.lock)),
                         [("input-removed", "alpha_read"), ("required-added", "alpha_write")])

    def test_capability_removed_or_effect_changed_is_refused(self) -> None:
        manifest = {"capabilities": [{"name": "alpha.survey", "effect": "draft"}]}
        self.assertEqual(codes(add_only.check(surface(manifest=manifest), self.lock)),
                         [("effect-changed", "alpha.survey"), ("missing", "alpha.record")])

    def test_lock_edited_to_hide_a_removal_is_refused(self) -> None:
        shrunk = SOURCE.split("@server.tool(name=")[0]
        hidden = copy.deepcopy(self.lock)
        del hidden["tools"]["alpha_write"]
        self.assertEqual(codes(add_only.check(surface(shrunk), hidden, self.lock)),
                         [("lock-rewritten", "alpha_write")])

    def test_lock_never_forgets_a_row(self) -> None:
        shrunk = SOURCE.split("@server.tool(name=")[0]
        self.assertIn("alpha_write", add_only.locked(surface(shrunk), self.lock)["tools"])


class Repository(unittest.TestCase):
    def test_served_surface_keeps_the_lock(self) -> None:
        lock = json.loads((ROOT / "tools" / "shipped.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["schema"], "add-only-lock/1")
        found = add_only.check(add_only.served(), lock)
        self.assertEqual(found, [], "\n".join(f"{f['code']} {f['name']}: {f['detail']}" for f in found))

    def test_the_lock_names_every_declared_tool(self) -> None:
        source = (ROOT / "observatory" / "engine" / "mcp" / "server.py").read_text(encoding="utf-8")
        self.assertEqual(len(add_only.tools_of(source)), source.count("@server.tool("))

    def test_agents_are_told_the_rule_and_the_command(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for needle in ("Add only, improve only", "python tools/check_add_only.py check",
                       "python tools/check_add_only.py lock", "tools/shipped.lock.json", "retired"):
            self.assertIn(needle, agents)


if __name__ == "__main__":
    unittest.main()
