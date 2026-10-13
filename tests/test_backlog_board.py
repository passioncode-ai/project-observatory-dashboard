"""tools/check_backlog.py — the engine board says only what stays true after a merge.

`docs/backlog.md` is the home of OBS rows (engine execution); product packages live on the
organization's board and are cited here as `parent: PB-nnn`. The lint keeps the board honest on
its own: one row per id, ids below the OBS register's next free number, no wording that goes stale
at merge ("in this PR", "to follow", "once … restarts"), no "done" with a remainder, every "done in
X.Y.Z" a release CHANGELOG.md has, and no private name in the board or CHANGELOG.md (the
public-release denylist). It also holds the board to the canonical format the organization's
register check reads (org-index `scripts/check_observatory_registers.py`, as merged in org-index
#67): one `## Board` section holding only the exact header, the separator and contiguous four-cell
rows; a Status cell that is exactly one closed form or `open`/`blocked` with an optional note; at
most one `parent: PB-nnn` in Source.

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

## Board

| ID | Item | Status | Source |
|---|---|---|---|
"""
CHANGELOG = ("# Changelog\n\n## Unreleased\n\n## 0.24.4 — 2026-10-12\n\n## 0.24.1 — 2026-10-12\n\n"
             "## 0.24.0 — 2026-10-12\n\n## 0.23.0 — 2026-10-10\n")

#: Status cells as the board at 9a3778f carried them.
AT_9A3778F = {
    "OBS-56": "done in 0.24.0 (#218) for the estate total; per-project limits from agents' presets remain open "
              "— `scan-spend` measures every door account's month",
    "OBS-61": "done in 0.24.0 (#219) — hooks (H11, `harness.hook_risk`), usage (H12), project skills (H13) and "
              "Codex skills (H14) read; settings-page controls remain",
    "OBS-63": "open — branch `agent/disk-hygiene`, pull request to follow; measured on one machine 2026-10-11",
    "OBS-65": "done in this PR (`agent/obs-65-identity-aliases`): `config/identifiers.json` (one alias, one target)",
}
#: The same rows in the canonical format: a closed status is exactly one form, its notes in Item.
CORRECTED = {
    "OBS-56": "done in 0.24.0 (#218)",
    "OBS-61": "open — partly done: hooks, usage, project and Codex skills in 0.24.0 (#219); settings-page controls "
              "remain (UX scenarios first)",
    "OBS-63": "done in 0.24.4 (#202, #229, #232, #234, #236, #238)",
    "OBS-65": "done in 0.24.0 (#203)",
}
#: Every closed and partial form of the grammar, and open/blocked with and without a note.
FORMS = {
    "OBS-01": "done", "OBS-02": "done in 0.24.0", "OBS-03": "done in 0.24.0 (#203)",
    "OBS-04": "done in 0.24.4 (#202, #229)", "OBS-05": "done — merged (#243)",
    "OBS-06": "done — merged (#243, #244), unreleased", "OBS-07": "cancelled", "OBS-08": "superseded by OBS-01",
    "OBS-09": "open", "OBS-10": "blocked", "OBS-11": "open — waits on the ADR", "OBS-12": "blocked: the operator's call",
    "OBS-13": "open; first packet first", "OBS-14": "open, raised 2026-10-06", "OBS-15": "open (a note)",
}


def load():
    spec = importlib.util.spec_from_file_location("check_backlog", ROOT / "tools/check_backlog.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def board(rows: dict[str, str], next_free: int = 84, extra: str = "", after: str = "") -> str:
    lines = [f"| {rid} | an engine task | {status} | parent: PB-140 |" for rid, status in rows.items()]
    return HEAD.format(next=next_free) + "\n".join(lines) + "\n" + extra + after


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

    def test_the_board_at_9a3778f_itself_fails_on_those_phrases(self):
        """The real file, when the checkout has the commit (a shallow CI clone may not)."""
        def show(path):
            run = subprocess.run(["git", "-C", str(ROOT), "show", f"9a3778f:{path}"], capture_output=True,
                                 text=True, timeout=60)
            return run.stdout if run.returncode == 0 else None
        text, changelog = show("docs/backlog.md"), show("CHANGELOG.md")
        if text is None or changelog is None:
            self.skipTest("9a3778f is not in this checkout")
        found = self.lint.check_board(text, changelog)
        for rid, said in (("OBS-63", "to follow"), ("OBS-65", "in this PR"), ("OBS-56", "remain"),
                          ("OBS-61", "remain")):
            self.assertTrue(any(p.startswith(rid) and said in p for p in found), (rid, said))
        self.assertTrue(any("## Board" in p for p in found), "an older board has no `## Board` section")

    def test_the_corrected_statuses_pass(self):
        self.assertEqual(self.problems(board(CORRECTED)), [])

    def test_remaining_in_the_item_of_a_done_row_is_not_a_remainder(self):
        # OBS-42 at 982f784: a skip described, nothing left to do; the note now lives in Item.
        text = HEAD.format(next=84) + ("| OBS-42 | an engine task; the one remaining skip is uncoverable by design "
                                       "| done in 0.23.0 | parent: PB-140 |\n")
        self.assertEqual(self.problems(text), [])

    def test_every_form_of_the_grammar_passes(self):
        self.assertEqual(self.problems(board(FORMS)), [])

    def test_a_closed_form_takes_no_note(self):
        # 42 of 90 rows on 7bcf331 carried prose after a closed form; the organization's check refuses them all.
        for status in ("done — the refusal was the key's", "done in 0.24.0 (#203): config/identifiers.json",
                       "done in 0.19.2 (a note)", "done — merged (#240), unreleased — on macOS a place",
                       "cancelled — a duplicate", "superseded by OBS-63 (operator decision)",
                       "superseded by OBS-63, OBS-64", "done in 0.23.0–0.24.4 (#202)", "done; released in 0.16.0"):
            found = self.problems(board({"OBS-01": status}))
            self.assertTrue(any(p.startswith("OBS-01") and "Item column" in p for p in found), (status, found))

    def test_a_status_cell_is_printable_ascii_without_emphasis_or_entities(self):
        for status in ("open, **raised 2026-10-06**", "open — `discard_fresh` deletes", "open &amp; waiting",
                       "open ~~struck~~", "open — `563e815`→`867e3fa`", "open\u00a0— a note", "dоne"):
            found = self.problems(board({"OBS-01": status}))
            self.assertTrue(any(p.startswith("OBS-01") and ("status" in p or "invisible" in p) for p in found),
                            (status, found))

    def test_keywords_are_case_insensitive_as_the_organization_reads_them(self):
        self.assertEqual(self.problems(board({"OBS-01": "Done in 0.24.0 (#1)", "OBS-02": "Open — a note"})), [])

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
        # `closed` is not a form: a duplicate is `superseded by OBS-n`, a dropped row `cancelled`.
        found = self.problems(board({"OBS-01": "shipped, mostly", "OBS-02": "superseded by OBS-04",
                                     "OBS-03": "closed — a duplicate", "OBS-04": "blocked — the operator's call",
                                     "OBS-05": "done — merged (#243), unreleased", "OBS-06": "cancelled",
                                     "OBS-07": "opened", "OBS-08": "open—no space"}))
        self.assertEqual([p.split(":")[0] for p in found], ["OBS-01", "OBS-03", "OBS-07", "OBS-08"], found)

    def test_done_in_a_release_the_changelog_does_not_have_fails(self):
        found = self.problems(board({"OBS-01": "done in 0.25.0 (#300)", "OBS-02": "done in 0.24.1 (#1)"}))
        self.assertEqual(len(found), 1, found)
        self.assertIn("0.25.0", found[0])

    def test_a_row_wider_than_the_header_fails(self):
        # A raw pipe inside a code span splits a GFM cell (the board's OBS-55 at 982f784).
        text = board({"OBS-01": "open"}, extra="| OBS-02 | `mode: unlimited | limited` | open | x |\n")
        self.assertTrue(any(p.startswith("OBS-02") and "cells" in p for p in self.problems(text)), self.problems(text))

    def test_an_escaped_pipe_is_refused_too(self):
        # `\\|` renders as a pipe on GitHub, but the organization's reader refuses any backslash in the
        # board; OBS-55 words the alternative instead (`unlimited` or `limited`).
        text = board({"OBS-01": "open"}, extra="| OBS-02 | `mode: unlimited \\| limited` | open | x |\n")
        self.assertTrue(any(p.startswith("OBS-02") and "backslash" in p for p in self.problems(text)),
                        self.problems(text))

    def test_html_and_control_characters_in_a_row_are_refused(self):
        for cell, why in (("a <details> block", "<"), ("a\x0bvertical tab", "control"),
                          ("a zero\u200bwidth space", "control")):
            text = board({"OBS-01": "open"}, extra=f"| OBS-02 | {cell} | open | x |\n")
            self.assertTrue(any(p.startswith("OBS-02") and why in p for p in self.problems(text)),
                            (cell, self.problems(text)))

    def test_a_row_that_is_not_canonical_is_refused(self):
        for line in ("|OBS-02 | tight | open | x |", "| OBS-02 | trailing | open | x | ", "OBS-02 | no pipe | open | x |"):
            found = self.problems(board({"OBS-01": "open"}, extra=line + "\n"))
            self.assertTrue(any("OBS-02" in p or "line 1" in p for p in found), (line, found))
            self.assertTrue(found, line)


    def test_a_planted_private_name_fails_the_denylist_and_is_not_printed(self):
        planted = "quiet-heron-agent"
        text = board({"OBS-01": "done in 0.24.0 (#1)"},
                     extra=f"| OBS-02 | the {planted} key moved | done in 0.24.0 (#1) | parent: PB-140 |\n")
        found = self.problems(text, deny=[planted])
        self.assertEqual([p.split(":")[0] for p in found], ["OBS-02"], found)
        self.assertIn("private-identifier", found[0])
        self.assertFalse(any(planted in p for p in found), "the lint printed the private value")
        self.assertEqual(self.problems(text), [], "without a denylist only the shape patterns apply")

    def test_a_planted_private_name_in_any_cell_fails_the_denylist(self):
        planted = "quiet-heron-agent"
        for cell in ("item", "status", "source"):
            cells = {"item": "an engine task", "status": "open", "source": "parent: PB-140"}
            cells[cell] = f"{cells[cell]} — {planted}" if cell != "source" else f"{planted} brief"
            text = board({"OBS-01": "open"},
                         extra=f"| OBS-02 | {cells['item']} | {cells['status']} | {cells['source']} |\n")
            found = self.problems(text, deny=[planted])
            self.assertTrue(any(p.startswith("OBS-02") and "private-identifier" in p for p in found), (cell, found))
            self.assertFalse(any(planted in p for p in found), "the lint printed the private value")

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
            b.write_text(board({"OBS-01": "done in 0.24.0 (#1)"},
                               extra="| OBS-02 | quiet-heron-agent | open | parent: PB-140 |\n"), encoding="utf-8")
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
        # Raw bytes: read_text would turn a CR or CRLF into LF and hide what the organization refuses.
        text = (ROOT / "docs/backlog.md").read_bytes().decode("utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_bytes().decode("utf-8")
        self.assertEqual(self.lint.check_board(text, changelog), [])

    def test_the_live_board_says_it_is_canonical_for_obs_rows(self):
        text = (ROOT / "docs/backlog.md").read_text(encoding="utf-8")
        self.assertIn("canonical for OBS rows", text.split("## Board", 1)[0])


class BoardSection(unittest.TestCase):
    """The organization's check reads the `## Board` section only, as an allowlist of lines."""

    def setUp(self):
        self.lint = load()

    def problems(self, text: str) -> list[str]:
        return self.lint.check_board(text, CHANGELOG)

    def test_the_canonical_board_passes(self):
        self.assertEqual(self.problems(board(CORRECTED, after="\n## Notes\n\nProse after the board.\n")), [])

    def test_a_board_without_the_heading_fails(self):
        found = self.problems(board(CORRECTED).replace("## Board\n", ""))
        self.assertTrue(any("## Board" in p for p in found), found)

    def test_a_board_without_its_heading_still_has_its_rows_read(self):
        found = self.problems(board(AT_9A3778F).replace("## Board\n", ""))
        self.assertTrue(any(p.startswith("OBS-63") and "to follow" in p for p in found), found)
        self.assertFalse(any("outside the board" in p for p in found), found)

    def test_a_second_board_heading_fails(self):
        found = self.problems(board(CORRECTED, after="\n## Board\n"))
        self.assertTrue(any("## Board" in p and "lines" in p for p in found), found)

    def test_the_header_and_separator_are_exact(self):
        for old, new in (("| ID | Item | Status | Source |", "| Id | Item | Status | Source |"),
                         ("| ID | Item | Status | Source |", "| ID | Item | Status | Source | Notes |"),
                         ("|---|---|---|---|", "|---|---|---|"), ("|---|---|---|---|", "|--|--|--|--|")):
            found = self.problems(board(CORRECTED).replace(old, new))
            self.assertTrue(any(p.startswith("docs/backlog.md line") for p in found), (new, found))

    def test_prose_between_the_heading_and_the_table_fails(self):
        found = self.problems(board(CORRECTED).replace("## Board\n", "## Board\n\nA sentence.\n"))
        self.assertTrue(any("header" in p for p in found), found)

    def test_text_after_the_table_in_the_section_fails(self):
        found = self.problems(board(CORRECTED, after="\nA note under the table.\n"))
        self.assertTrue(any("after the board's table" in p for p in found), found)

    def test_a_blank_line_inside_the_table_ends_it(self):
        text = board({"OBS-01": "open"}, extra="\n| OBS-02 | an engine task | open | x |\n")
        self.assertTrue(any("after the board's table" in p for p in self.problems(text)), self.problems(text))

    def test_an_obs_row_outside_the_section_fails(self):
        for outside in ("| OBS-02 | an engine task | open | x |", "| **OBS-02** | x | open | x |",
                        "| [OBS-02](#x) | x |"):
            found = self.problems(board(CORRECTED, after=f"\n## Archive\n\n{outside}\n"))
            self.assertTrue(any("outside the board" in p for p in found), (outside, found))

    def test_a_fence_or_html_block_above_the_board_fails(self):
        for above in ("```\n", "<!--\n", "<details>\n\n"):
            found = self.problems(board(CORRECTED).replace("## Board\n", above + "## Board\n"))
            self.assertTrue(found, above)

    def test_a_board_with_no_rows_fails(self):
        found = self.problems(HEAD.format(next=84))
        self.assertTrue(any("no OBS rows" in p for p in found), found)


class Parents(unittest.TestCase):
    def setUp(self):
        self.lint = load()

    def row(self, source: str) -> list[str]:
        text = HEAD.format(next=84) + f"| OBS-01 | an engine task | open | {source} |\n"
        return self.lint.check_board(text, CHANGELOG)

    def test_one_parent_or_none_passes(self):
        for source in ("parent: PB-140", "parent: PB-140; also PB-138", "[run record](runs/x/README.md)",
                       "Parent: PB-7"):
            self.assertEqual(self.row(source), [], source)

    def test_two_parents_or_a_parent_without_a_package_fail(self):
        for source in ("parent: PB-140, parent: PB-138", "parent: OBS-63", "parent:", "parent: PB-14x"):
            found = self.row(source)
            self.assertTrue(any(p.startswith("OBS-01") and "parent" in p for p in found), (source, found))

class Hardening(unittest.TestCase):
    """Round-1 findings on #277: what the organization's reader refuses and an earlier lint let through.

    Each case below also pins one line of the lint, so reverting that line turns a test red
    (the planted reverts of round 1: the dash class, CR, NBSP, a comment opened mid-line, a
    table's lazy continuation)."""

    def setUp(self):
        self.lint = load()

    def problems(self, text: str) -> list[str]:
        return self.lint.check_board(text, CHANGELOG)

    def with_row(self, item: str = "an engine task", status: str = "open", source: str = "x") -> list[str]:
        return self.problems(board({"OBS-01": "open"}, extra=f"| OBS-02 | {item} | {status} | {source} |\n"))

    # F1 — a CR anywhere, as the organization reads the raw document.
    def test_a_crlf_board_is_refused(self):
        found = self.problems(board(CORRECTED).replace("\n", "\r\n"))
        self.assertTrue(any("CR" in p for p in found), found)

    def test_a_cr_above_the_board_is_refused(self):
        found = self.problems("# B\rx\n" + board(CORRECTED))
        self.assertTrue(any("CR" in p and "line 1" in p for p in found), found)

    def test_the_command_reads_raw_bytes_so_a_crlf_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            b, c = Path(tmp, "backlog.md"), Path(tmp, "CHANGELOG.md")
            b.write_bytes(board(CORRECTED).replace("\n", "\r\n").encode("utf-8"))
            c.write_text(CHANGELOG, encoding="utf-8")
            env = {**os.environ}
            env.pop("OBSERVATORY_PRIVATE_DENYLIST", None)
            run = subprocess.run([sys.executable, str(ROOT / "tools/check_backlog.py"), "--board", str(b),
                                  "--changelog", str(c)], capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(run.returncode, 1, run.stderr)
            self.assertIn("CR", run.stderr)

    # F2 — a line starting with `<` opens an HTML block until the next blank line.
    def test_a_line_starting_with_lt_directly_above_the_board_hides_it(self):
        for above in ("<3 not a tag\n", "   < spaced\n", "<x\nstill inside\n"):
            found = self.problems(board(CORRECTED).replace("## Board\n", above + "## Board\n"))
            self.assertTrue(any("HTML block" in p and "## Board" in p for p in found), (above, found))

    def test_a_lt_line_closed_by_a_blank_line_does_not_hide_the_board(self):
        text = board(CORRECTED).replace("## Board\n", "<3 not a tag\n\n## Board\n")
        self.assertEqual(self.problems(text), [])

    def test_a_comment_opened_mid_line_above_the_board_is_refused(self):
        # Closed or not: nothing HTML sits above the board.
        for above in ("Prose <!-- a note -->\n\n", "Prose <!-- open\n\n-->\n\n"):
            found = self.problems(board(CORRECTED).replace("## Board\n", above + "## Board\n"))
            self.assertTrue(any("HTML" in p for p in found), (above, found))

    # F3 — ids are ASCII digits; one number is one id however it is padded.
    def test_an_id_with_non_ascii_digits_is_refused(self):
        found = self.problems(board({"OBS-01": "open"}, extra="| OBS-١ | an engine task | open | x |\n"))
        self.assertTrue(any("canonical row" in p for p in found), found)

    def test_an_id_padded_beyond_the_two_digit_form_is_refused(self):
        for rid in ("OBS-001", "OBS-0", "OBS-010"):
            found = self.problems(board({"OBS-01": "open"}, extra=f"| {rid} | an engine task | open | x |\n"))
            self.assertTrue(any("canonical row" in p for p in found), (rid, found))

    def test_obs_01_beside_obs_1_is_one_id_written_twice(self):
        found = self.problems(board({"OBS-01": "open"}, extra="| OBS-1 | an engine task | open | x |\n"))
        self.assertTrue(any("twice" in p for p in found), found)

    def test_a_parent_with_non_ascii_digits_is_refused(self):
        found = self.with_row(source="parent: PB-١٣")
        self.assertTrue(any(p.startswith("OBS-02") and "parent" in p for p in found), found)

    def test_a_superseded_successor_with_non_ascii_digits_is_refused(self):
        found = self.with_row(status="superseded by OBS-١")
        self.assertTrue(any(p.startswith("OBS-02") for p in found), found)

    # F4 — no control, format, separator or invisible character in any cell.
    def test_invisible_characters_are_refused_in_item_and_source(self):
        for ch in ("­", "͏", "᠎", "ㅤ", "￹", "\U000e0041", "ᅟ", "ᅠ", "ﾠ",
                   " ", " ", " ", " ", "​", "‮", "\t", "\x0b", "️"):
            for cell in ("item", "source"):
                kw = {cell: f"a{ch}b"}
                found = self.with_row(**kw)
                self.assertTrue(any(p.startswith("OBS-02") and "invisible" in p for p in found),
                                (hex(ord(ch)), cell, found))

    def test_visible_non_ascii_text_passes_in_item_and_source(self):
        self.assertEqual(self.with_row(item="«готовим релиз» — 12/12 × 0.5 → é", source="parent: PB-137; ü"), [])

    # F5 — the dash class of the forms: em dash, en dash and hyphen all read.
    def test_every_dash_of_the_forms_reads(self):
        for status in ("done – merged (#1)", "done - merged (#1), unreleased", "open – a note", "blocked - a note",
                       "done — merged (#1)"):
            self.assertEqual(self.with_row(status=status), [], status)

    # Round 2 — N1: a successor is an existing row, spelled exactly as that row spells it.
    def test_a_successor_must_be_an_existing_row_spelled_as_written(self):
        rows = {"OBS-01": "open", "OBS-02": "superseded by OBS-01", "OBS-03": "superseded by OBS-1",
                "OBS-04": "superseded by OBS-77", "OBS-05": "Superseded by obs-01"}
        found = self.problems(board(rows))
        refused = sorted({p.split(":")[0] for p in found if "successor" in p})
        self.assertEqual(refused, ["OBS-03", "OBS-04", "OBS-05"], found)

    def test_a_successor_written_later_on_the_board_is_found(self):
        self.assertEqual(self.problems(board({"OBS-01": "superseded by OBS-02", "OBS-02": "open"})), [])

    # Round 2 — N3: unassigned default-ignorable code points (Cn) are invisible too.
    def test_default_ignorable_code_points_are_refused_assigned_or_not(self):
        for cp in (0x2065, 0xFFF0, 0xFFF8, 0xE0000, 0xE0002, 0xE001F, 0xE0080, 0xE0FFF, 0x1BCA0, 0x1D173):
            found = self.with_row(item=f"a{chr(cp)}b")
            self.assertTrue(any(p.startswith("OBS-02") and "invisible" in p for p in found), (hex(cp), found))

    def test_a_lazy_continuation_of_a_table_outside_the_board_is_an_obs_row(self):
        found = self.problems(board(CORRECTED, after="\n## Archive\n\n| a | b |\nOBS-02 continues the row\n"))
        self.assertTrue(any("outside the board" in p for p in found), found)


if __name__ == "__main__":
    unittest.main()
