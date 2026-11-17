#!/usr/bin/env python3
"""OBS-614: one append-only journal writer.

`journal_writer.append` writes one line per record, owner-only, never through a symbolic link, rotated by
`log_policy`'s rule when the caller asks; each module that keeps a journal calls it instead of its own helper.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
for where in (ROOT, ROOT / "collectors", ROOT / "tools", ROOT / "store", ROOT / "node"):
    sys.path.insert(0, str(where))

import journal_writer  # noqa: E402
import log_policy  # noqa: E402

POSIX = os.name == "posix"


class Writer(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.file = self.tmp / "logs" / "j.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lines(self):
        return self.file.read_text(encoding="utf-8").splitlines()

    def test_one_line_per_record_in_order(self):
        journal_writer.append(self.file, [{"b": 1, "a": "ä"}, {"c": 2}])
        journal_writer.append(self.file, [{"d": 3}])
        self.assertEqual([json.loads(x) for x in self.lines()], [{"b": 1, "a": "ä"}, {"c": 2}, {"d": 3}])
        self.assertIn("ä", self.lines()[0], "not escaped by default")

    def test_encoding_options_are_the_callers(self):
        journal_writer.append(self.file, [{"b": 1, "a": "ä"}], sort_keys=True, ensure_ascii=True)
        self.assertEqual(self.lines(), ['{"a": "\\u00e4", "b": 1}'])

    def test_lines_as_given(self):
        journal_writer.append_lines(self.file, ["x", "y"])
        self.assertEqual(self.lines(), ["x", "y"])

    def test_a_line_with_a_newline_is_refused_and_nothing_written(self):
        with self.assertRaises(ValueError):
            journal_writer.append_lines(self.file, ["ok", "two\nlines"])
        self.assertFalse(self.file.exists())

    def test_nothing_to_write_creates_nothing(self):
        journal_writer.append(self.file, [])
        self.assertFalse(self.file.exists())

    @unittest.skipUnless(POSIX, "permission bits")
    def test_owner_only_file_and_folder(self):
        journal_writer.append(self.file, [{"a": 1}])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file.parent.stat().st_mode), 0o700)

    @unittest.skipUnless(POSIX, "permission bits")
    def test_a_loose_existing_file_is_tightened(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("old\n", encoding="utf-8")
        os.chmod(self.file, 0o644)
        journal_writer.append_lines(self.file, ["new"])
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(self.lines(), ["old", "new"])

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_never_through_a_symbolic_link(self):
        target = self.tmp / "elsewhere.jsonl"
        target.write_text("", encoding="utf-8")
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(target)
        with self.assertRaises(OSError):
            journal_writer.append_lines(self.file, ["x"])
        self.assertEqual(target.read_text(encoding="utf-8"), "")

    def test_a_directory_is_no_journal(self):
        self.file.mkdir(parents=True)
        with self.assertRaises(OSError):
            journal_writer.append_lines(self.file, ["x"])

    def test_rotation_is_log_policys_and_only_when_asked(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("x" * 10 + "\n", encoding="utf-8")
        with mock.patch.object(log_policy, "MAX_BYTES", 5):
            journal_writer.append_lines(self.file, ["kept"])
            self.assertFalse(self.file.with_name("j.jsonl.1").exists())
            with mock.patch.object(log_policy, "rotate", wraps=log_policy.rotate) as rotate:
                journal_writer.append_lines(self.file, ["after"], rotate=True)
            rotate.assert_called_once()
        self.assertTrue(self.file.with_name("j.jsonl.1").exists())
        self.assertEqual(self.lines(), ["after"])

    def test_fsync_on_request(self):
        with mock.patch.object(os, "fsync") as fsync:
            journal_writer.append_lines(self.file, ["a"])
            fsync.assert_not_called()
            journal_writer.append_lines(self.file, ["b"], fsync=True)
            fsync.assert_called_once()

    def test_one_write_per_call(self):
        """All the records of one call land in one write, so two writers never interleave inside a call."""
        real = os.write
        with mock.patch.object(os, "write", side_effect=real) as write:
            journal_writer.append(self.file, [{"a": i} for i in range(50)])
        self.assertEqual(write.call_count, 1)


#: The modules whose journal helper is now a call to the writer (see redact-and-journal.md for the rest).
MOVED = ("configuration_door.py", "workspace_profile.py", "engine_update.py", "retrieval_audit.py",
         "memory_access.py", "update_events.py", "ops_acts.py", "tracked.py", "review_policy.py",
         "store/workflow.py", "store/forget.py", "node/shares.py", "tools/daily_doctor.py", "tools/cleanup.py",
         "tools/scrub_companion.py", "tools/hung_commands.py", "tools/session_start.py",
         "store_faults.py", "companion_faults.py")


class Moved(unittest.TestCase):
    def test_a_moved_module_keeps_no_append_of_its_own(self):
        for name in MOVED:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertFalse("O_APPEND" in text, name)
            self.assertIsNone(re.search(r"""open\([^)]*["']a["']""", text), name)
            self.assertTrue("journal_writer" in text, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
