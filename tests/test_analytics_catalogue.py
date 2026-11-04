"""The event catalogue (docs/analytics-events.md) and the code agree (OBS-324).

An event's name is a time series, so the catalogue is held to the code both ways: every event in
`usage_analytics.EVENTS` has a row with its properties, every row names an event the code knows,
and every emit in the engine (`record("<event>"`) is listed with its exact `file:line`. A change that
adds, moves or removes an emit without touching the catalogue fails here.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "observatory" / "engine"
CATALOGUE = ROOT / "docs" / "analytics-events.md"
EMIT = re.compile(r'(?:^|[^A-Za-z0-9_.])(?:UA\.)?record\(\s*"([a-z_]+)"')


def code_events() -> dict[str, set[str]]:
    """EVENTS from usage_analytics.py, read without importing the engine."""
    tree = ast.parse((ENGINE / "usage_analytics.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "EVENTS":
            return {k.value: {kk.value for kk in v.keys} for k, v in zip(node.value.keys, node.value.values)}
    raise AssertionError("EVENTS not found in usage_analytics.py")


def emits() -> list[tuple[str, str]]:
    found = []
    for path in sorted(ENGINE.rglob("*.py")):
        if "tests" in path.relative_to(ENGINE).parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "store_faults.record" in line:
                continue
            match = EMIT.search(line)
            if match:
                found.append((match.group(1), f"{path.relative_to(ROOT).as_posix()}:{number}"))
    return found


class Catalogue(unittest.TestCase):
    def setUp(self):
        self.text = CATALOGUE.read_text(encoding="utf-8")
        self.events = code_events()
        rows = re.findall(r"^\| `([a-z_]+)` \| (live|retired)[^|]*\| ([0-9-]+) \| ([^|]+) \| ([^|]+) \|$",
                          self.text, re.M)
        self.rows = {name: {"status": status, "where": where, "props": set(re.findall(r"`([a-z_]+)`", props))}
                     for name, status, _added, where, props in rows}

    def test_every_event_in_the_code_has_its_row_and_properties(self):
        for name, props in self.events.items():
            with self.subTest(event=name):
                self.assertIn(name, self.rows)
                self.assertEqual(self.rows[name]["status"], "live")
                declared = self.rows[name]["props"] - {"cli", "mcp", "tick", "dashboard"}
                self.assertEqual(declared, props)
                for prop in props:
                    self.assertRegex(self.text, rf"(?m)^\| `{name}` \| `{prop}` \| ")

    def test_every_row_names_an_event_the_code_knows(self):
        live = {n for n, r in self.rows.items() if r["status"] == "live"}
        self.assertEqual(live - set(self.events), set())

    def test_every_emit_is_listed_at_its_line(self):
        found = emits()
        self.assertTrue(found, "no emit found: the pattern no longer matches the code")
        for event, where in found:
            with self.subTest(emit=where):
                self.assertIn(event, self.events, f"{where} emits an event the catalogue in code lacks")
                self.assertIn(f"`{where}`", self.rows[event]["where"], f"move the catalogue's line for {event}")
        listed = set(re.findall(r"`(observatory/engine/[a-z_/]+\.py):(\d+)`", self.text))
        for path, line in listed:
            text = (ROOT / path).read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(text), int(line), f"{path}:{line} is past the end of the file")
            self.assertRegex(text[int(line) - 1], r"record\(|_notice\(|_viewed\(", f"{path}:{line} is not an emit or a notice")

    def test_no_em_dash(self):
        self.assertNotIn("\u2014", self.text)


if __name__ == "__main__":
    unittest.main()
