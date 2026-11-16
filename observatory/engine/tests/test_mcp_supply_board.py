#!/usr/bin/env python3
"""The supply-chain findings of the board (OBS-555, CP-09): `mcp.risky_command` and `mcp.tool_changed`, driven through the
real builder on a synthetic estate (a LEGACY suite: it needs the populated workspace the bootstrap makes)."""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("", "tests"):
    sys.path.insert(0, str(ROOT / sub) if sub else str(ROOT))

import mcp_supply as S                                                      # noqa: E402


class Findings(unittest.TestCase):
    def test_the_board_names_the_rule_the_server_and_the_changed_tool_and_no_command(self):
        import test_finding_rules as board
        doc = {"servers": [
            {"id": "mcp:claude/user/alpha", "name": "alpha", "agent": "claude", "liveness": "connected",
             "key_in_url": False, "risky": ["unpinned-runner", "fetch-execute"]},
            {"id": "mcp:cursor/user/fine", "name": "fine", "agent": "cursor", "liveness": "connected",
             "key_in_url": False, "risky": []}],
            "own_declared": True, "totals": {"declarations": 2, "distinct_servers": 2, "by_liveness": {},
                                              "in_one_agent_only": 2, "key_in_url": 0}}
        pins = {"schema": S.SCHEMA, "servers": {"claude:alpha": {
            "tools": {"read": "sha256:" + "a" * 64},
            "pending": {"read": {"change": "changed", "old": "sha256:" + "a" * 64, "new": "sha256:" + "b" * 64,
                                 "seen_at": "2026-11-16T10:00:00Z"},
                        "send": {"change": "added", "old": None, "new": "sha256:" + "c" * 64,
                                 "seen_at": "2026-11-16T10:00:00Z"}}}}}
        rows = board.sandbox(registry={"mcp-servers.json": lambda d: (d.clear(), d.update(doc))},
                             state={"state/mcp-pins": pins})
        risky = board.rows(rows, "mcp.risky_command")
        self.assertEqual(sorted(r["severity"] for r in risky), ["critical", "warning"])
        self.assertTrue(all("alpha" in r["title"] for r in risky), str(risky)[:300])
        self.assertTrue(all(r["subject"].startswith("mcp:claude/alpha/") for r in risky))
        changed = board.rows(rows, "mcp.tool_changed")
        self.assertEqual(len(changed), 2, str(changed)[:300])
        self.assertTrue(any("read" in r["title"] and "alpha" in r["title"] for r in changed))
        self.assertTrue(any("added the tool send" in r["title"] for r in changed))
        self.assertFalse([r for r in risky + changed if "fine" in r["title"]])
        self.assertNotIn("npx", json.dumps(risky + changed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
