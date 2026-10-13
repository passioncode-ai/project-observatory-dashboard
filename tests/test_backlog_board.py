"""tools/check_backlog.py — the engine board says only what stays true after a merge.

`docs/backlog.md` is the home of OBS rows (engine execution); product packages live on the
organization's board and are cited here as `PB-nnn`. The lint keeps the board honest on its own:
one row per id, ids below the OBS register's next free number, a status from the vocabulary,
no wording that goes stale at merge ("in this PR", "to follow", "once … restarts"), no "done"
with a remainder, every "done in X.Y.Z" a release CHANGELOG.md has, every row the header's
width, and no private name in the board or CHANGELOG.md (the public-release denylist).

The status cells below are copied from the board at 9a3778f (OBS-56, OBS-61, OBS-63, OBS-65);
the item text around them is neutral. The private name planted for the denylist case is a
made-up one: a real private name in a public test file would be the leak this guards against.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

HEAD = """# Local backlog

Canonical for OBS rows. **Next free ID:** `OBS-{next}`

| ID | Item | Status | Source |
|---|---|---|---|
"""
CHANGELOG = "# Changelog\n\n## Unreleased\n\n## 0.24.1 — 2026-10-12\n\n## 0.24.0 — 2026-10-12\n\n## 0.23.0 — 2026-10-10\n"

#: Status cells as the board at 9a3778f carried them.
AT_9A3778F = {
    "OBS-56": "done in 0.24.0 (#218) for the estate total; per-project limits from agents' presets remain open "
              "— `scan-spend` measures every door account's month",
    "OBS-61": "done in 0.24.0 (#219) — hooks (H11, `harness.hook_risk`), usage (H12), project skills (H13) and "
              "Codex skills (H14) read; settings-page controls remain",
    "OBS-63": "open — branch `agent/disk-hygiene`, pull request to follow; measured on one machine 2026-10-11",
    "OBS-65": "done in this PR (`agent/obs-65-identity-aliases`): `config/identifiers.json` (one alias, one target)",
}
#: The same rows as dossier KL §L-a corrects them.
CORRECTED = {
    "OBS-56": "open — partly done: the estate total in 0.24.0 (#218); per-project limits from presets remain",
    "OBS-61": "open — partly done: hooks, usage, project and Codex skills in 0.24.0 (#219); settings-page controls "
              "remain (UX scenarios first)",
    "OBS-63": "done in 0.23.0–0.24.1 (#202, #229, #232); PB-140",
    "OBS-65": "done in 0.24.0 (#203)",
}


def load():
    spec = importlib.util.spec_from_file_location("check_backlog", ROOT / "tools/check_backlog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def board(rows: dict[str, str], next_free: int = 84, extra: str = "") -> str:
    lines = [f"| {rid} | an engine task | {status} | PB-140 |" for rid, status in rows.items()]
    return HEAD.format(next=next_free) + "\n".join(lines) + "\n" + extra


class BoardLint(unittest.TestCase):
    def setUp(self):
        self.lint = load()

    def problems(self, text: str, **kw) -> list[str]:
        return self.lint.check_board(text, CHANGELOG, **kw)

    def test_the_9a3778f_statuses_fail_on_what_goes_stale_at_merge(self):
        found = self.problems(board(AT_9A3778F))
        joined = "\n".join(found)
        self.assertIn("pull request to follow", joined)
        self.assertTrue(any(p.startswith("OBS-65") and "in this PR" in p for p in found), found)
        for rid in ("OBS-56", "OBS-61"):
            self.assertTrue(any(p.startswith(rid) and "remain" in p for p in found), (rid, found))

    def test_the_corrected_statuses_pass(self):
        self.assertEqual(self.problems(board(CORRECTED)), [])

    def test_remaining_in_a_done_status_is_not_a_remainder(self):
        # OBS-42 at 982f784: a skip described, nothing left to do.
        text = board({"OBS-42": "done in 0.23.0: the one remaining skip is uncoverable by design"})
        self.assertEqual(self.problems(text), [])

    def test_once_it_restarts_is_stale_wording(self):
        found = self.problems(board({"OBS-75": "done — the old key is revoked once the agent's service restarts"}))
        self.assertTrue(any("once" in p and "restart" in p for p in found), found)

    def test_an_id_written_twice_fails(self):
        # #209 rebased on main: its OBS-76 row beside main's OBS-76.
        text = board({"OBS-76": "open — first packet OE-00 (the ADR)"},
                     extra="| OBS-76 | scan_heroku --only replaces the whole raw scan | open | #209 |\n")
        self.assertTrue(any("OBS-76" in p and "twice" in p for p in self.problems(text)), self.problems(text))

    def test_an_id_at_or_above_the_next_free_number_fails(self):
        found = self.problems(board({"OBS-84": "open"}, next_free=84))
        self.assertTrue(any(p.startswith("OBS-84") and "reserve" in p for p in found), found)

    def test_a_board_without_the_register_line_cannot_reserve(self):
        text = board({"OBS-01": "open"}).replace("**Next free ID:** `OBS-84`", "")
        self.assertTrue(any("Next free ID" in p for p in self.problems(text)), self.problems(text))

    def test_a_status_outside_the_vocabulary_fails(self):
        found = self.problems(board({"OBS-01": "shipped, mostly", "OBS-02": "superseded by OBS-63 (decision)",
                                     "OBS-03": "closed — a duplicate", "OBS-04": "blocked — the operator's call",
                                     "OBS-05": "done — merged (#243), unreleased"}))
        self.assertEqual([p.split(":")[0] for p in found], ["OBS-01"], found)

    def test_done_in_a_release_the_changelog_does_not_have_fails(self):
        found = self.problems(board({"OBS-01": "done in 0.25.0 (#300)", "OBS-02": "done in 0.23.0–0.24.1 (#1)"}))
        self.assertEqual(len(found), 1, found)
        self.assertIn("0.25.0", found[0])

    def test_a_row_wider_than_the_header_fails(self):
        # A raw pipe inside a code span splits a GFM cell (the board's OBS-55 at 982f784).
        text = board({"OBS-01": "open"}, extra="| OBS-02 | `mode: unlimited | limited` | open | x |\n")
        self.assertTrue(any(p.startswith("OBS-02") and "cells" in p for p in self.problems(text)), self.problems(text))

    def test_a_planted_private_name_fails_the_denylist_and_is_not_printed(self):
        planted = "quiet-heron-agent"
        text = board({"OBS-01": f"done in 0.24.0 (#1) — the {planted} key moved"})
        found = self.problems(text, deny=[planted])
        self.assertTrue(any(p.startswith("OBS-01") and "private-identifier" in p for p in found), found)
        self.assertFalse(any(planted in p for p in found), "the lint printed the private value")
        self.assertEqual(self.problems(text), [], "without a denylist only the shape patterns apply")

    def test_the_changelog_is_scanned_too(self):
        found = self.lint.check_board(board(CORRECTED), CHANGELOG + "\nmoved from /" + "Users/someone/x\n")
        self.assertTrue(any(p.startswith("CHANGELOG.md") and "personal-machine-path" in p for p in found), found)

    def test_the_privacy_scan_sees_a_name_that_ends_a_sentence(self):
        scan = self.lint._privacy().scan_text
        for text in ("First use: quiet-heron-agent.", "quiet-heron-agent, then", "(quiet-heron-agent)",
                     "quiet-heron-agent.git"):
            self.assertEqual(scan(text, ["quiet-heron-agent"]), {"private-identifier": 1}, text)
        for text in ("quiet-heron-agent.example.com", "quiet-heron-agent-two", "xquiet-heron-agent"):
            self.assertEqual(scan(text, ["quiet-heron-agent"]), {}, text)

    def test_a_denylisted_name_in_the_changelog_alone_fails(self):
        # No shape pattern can see a name: only the denylist scan over CHANGELOG.md does.
        planted = "quiet-heron-agent"
        found = self.lint.check_board(board(CORRECTED), CHANGELOG + f"\nFirst use: {planted}.\n", deny=[planted])
        self.assertEqual([p.split(":")[0] for p in found], ["CHANGELOG.md"], found)
        self.assertIn("private-identifier", found[0])
        self.assertFalse(any(planted in p for p in found), "the lint printed the private value")

    def test_a_denylisted_name_in_the_board_prose_alone_fails(self):
        # The header and notes outside the rows are public too.
        planted = "quiet-heron-agent"
        text = board(CORRECTED).replace("Canonical for OBS rows.", f"Canonical for OBS rows; see {planted}.")
        found = self.problems(text, deny=[planted])
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("docs/backlog.md (outside the rows)"), found)
        self.assertFalse(any(planted in p for p in found), "the lint printed the private value")

    def test_the_command_reads_a_denylist_file_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, c, d = Path(tmp, "backlog.md"), Path(tmp, "CHANGELOG.md"), Path(tmp, "deny.json")
            b.write_text(board({"OBS-01": "done in 0.24.0 (#1) — quiet-heron-agent"}), encoding="utf-8")
            c.write_text(CHANGELOG, encoding="utf-8")
            d.write_text(json.dumps(["quiet-heron-agent"]), encoding="utf-8")
            run = subprocess.run([sys.executable, str(ROOT / "tools/check_backlog.py"), "--board", str(b),
                                  "--changelog", str(c), "--private-denylist", str(d)],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertNotIn("quiet-heron-agent", run.stdout + run.stderr)
            env = {**os.environ}
            env.pop("OBSERVATORY_PRIVATE_DENYLIST", None)
            clean = subprocess.run([sys.executable, str(ROOT / "tools/check_backlog.py"), "--board", str(b),
                                    "--changelog", str(c)], capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertIn("no local denylist", clean.stdout + clean.stderr)

    def test_the_register_pattern_is_the_one_agent_sync_reserves_from(self):
        config = json.loads((ROOT / ".claude/agent-sync.json").read_text(encoding="utf-8"))
        spec = config["idRegisters"]["OBS"]
        self.assertEqual(spec["file"], "docs/backlog.md")
        self.assertEqual(spec["pattern"], self.lint.NEXT_FREE.pattern)

    def test_the_live_board_is_clean(self):
        """The live board must remain reservable and meet the same rules as fixtures."""
        text = (ROOT / "docs/backlog.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(self.lint.check_board(text, changelog), [])


if __name__ == "__main__":
    unittest.main()
