#!/usr/bin/env python3
"""`osprivacy` keeps the privacy boundary on every OS (docs/design/WINDOWS-LINUX.md, W2b).

The same assertions run on POSIX (mode bits) and on the Windows CI job (the file's ACL): a file
made private is private, a file shared with everyone is not, a descriptor says what its path
says, `open` writes bytes unchanged and refuses a link.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import osprivacy  # noqa: E402


def share_with_everyone(path: Path) -> None:
    """Give every account read and write access, as a careless copy would."""
    if os.name == "nt":
        subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:(M)"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.chmod(path, 0o666)


class Privacy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.file = self.dir / "secret"
        self.file.write_bytes(b"value")

    def test_a_file_made_private_is_private(self):
        share_with_everyone(self.file)
        osprivacy.make_private(self.file)
        self.assertTrue(osprivacy.private(self.file), osprivacy.explain(self.file))
        self.assertFalse(osprivacy.others_may_write(self.file), osprivacy.explain(self.file))
        self.assertTrue(osprivacy.owned_by_me(self.file), osprivacy.explain(self.file))
        self.assertIn(osprivacy.describe(self.file), ("0600", "owner-only"))

    def test_a_file_shared_with_everyone_is_not(self):
        osprivacy.make_private(self.file)
        share_with_everyone(self.file)
        self.assertFalse(osprivacy.private(self.file), osprivacy.explain(self.file))
        self.assertTrue(osprivacy.others_may_write(self.file), osprivacy.explain(self.file))
        self.assertNotIn(osprivacy.describe(self.file), ("0600", "owner-only"))

    def test_a_folder_made_private_passes_it_on(self):
        folder = self.dir / "state"
        folder.mkdir()
        osprivacy.make_private(folder)
        self.assertTrue(osprivacy.private(folder), osprivacy.explain(folder))
        inside = folder / "new"
        inside.write_bytes(b"x")
        if os.name == "nt":  # inherited from the protected list
            self.assertTrue(osprivacy.private(inside), osprivacy.explain(inside))

    def test_a_descriptor_says_what_its_path_says(self):
        for prepare in (osprivacy.make_private, share_with_everyone):
            prepare(self.file)
            fd = osprivacy.open(self.file, os.O_RDONLY)
            try:
                self.assertEqual(osprivacy.private(fd), osprivacy.private(self.file))
                self.assertEqual(osprivacy.owned_by_me(fd), osprivacy.owned_by_me(self.file))
            finally:
                os.close(fd)

    def test_making_a_descriptor_private(self):
        share_with_everyone(self.file)
        fd = osprivacy.open(self.file, os.O_RDWR)
        try:
            osprivacy.make_private(fd)
            self.assertTrue(osprivacy.private(fd), osprivacy.explain(fd))
        finally:
            os.close(fd)

    def test_open_writes_bytes_unchanged(self):
        # Without O_BINARY a Windows descriptor is in text mode and LF becomes CRLF.
        target = self.dir / "bytes"
        fd = osprivacy.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | osprivacy.NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(b"a\nb\r\nc\x1a d")
        self.assertEqual(target.read_bytes(), b"a\nb\r\nc\x1a d")
        fd = osprivacy.open(target, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        try:
            self.assertEqual(os.read(fd, 100), b"a\nb\r\nc\x1a d")
        finally:
            os.close(fd)

    def test_open_refuses_a_link_when_asked(self):
        link = self.dir / "link"
        try:
            os.symlink(self.file, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"this account cannot create a symbolic link: {exc}")
        with self.assertRaises(OSError):
            os.close(osprivacy.open(link, os.O_RDONLY | osprivacy.NOFOLLOW))
        os.close(osprivacy.open(link, os.O_RDONLY))  # and follows it when not asked

    def test_a_folder_descriptor_exists_only_on_posix(self):
        if os.name == "nt":
            with self.assertRaises(NotImplementedError):
                osprivacy.open(self.dir, os.O_RDONLY | osprivacy.DIRECTORY)
        else:
            os.close(osprivacy.open(self.dir, os.O_RDONLY | osprivacy.DIRECTORY))


class RealHome(unittest.TestCase):
    def test_the_real_home_is_an_existing_folder(self):
        home = osprivacy.real_home()
        self.assertIsNotNone(home)
        self.assertTrue(home.is_dir(), home)

    def test_the_environment_does_not_move_it(self):
        # A test runner points HOME (USERPROFILE on Windows) at a sandbox; the account's real
        # home is what tells the two apart, so it must not follow the variable.
        before = osprivacy.real_home()
        with tempfile.TemporaryDirectory() as sandbox, \
                mock.patch.dict(os.environ, {"HOME": sandbox, "USERPROFILE": sandbox}):
            self.assertEqual(osprivacy.real_home(), before)
            self.assertNotEqual(Path.home().resolve(), before.resolve())


class Boundary(unittest.TestCase):
    #: Modules that only run on macOS, under launchd or for the Mac app — W4 gives them a
    #: scheduler per OS; until then `os.getuid` there names a launchd domain.
    MACOS_ONLY = {"tools/install_launchd.py", "app_update.py", "tools/serverd.py",
                  "engine_update.py", "agent/assistant.py"}
    FLAGS = re.compile(r"os\.O_(NOFOLLOW|DIRECTORY|NONBLOCK)\b|getattr\(os,\s*['\"]O_|\bos\.open\(")
    MODES = re.compile(r"st_mode\s*&\s*0o0[0-7][0-7]\b|S_IMODE\([^)]*\)\s*&\s*0o0|\bmode\s*&\s*0o0[0-7][0-7]\b|\bst_uid\b")

    def offenders(self, pattern, skip=frozenset()):
        found = []
        for f in ROOT.rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if f.name == "osprivacy.py" or rel.startswith("tests/") or rel in skip:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if text.startswith("# Vendored from"):
                continue  # upstream kit: docs/design/WINDOWS-LINUX.md, "Dependencies"
            for n, line in enumerate(text.splitlines(), 1):
                if pattern.search(line.split("#")[0]):
                    found.append(f"{rel}:{n}")
        return found

    def test_every_open_goes_through_osprivacy(self):
        # O_NOFOLLOW, O_DIRECTORY and O_NONBLOCK do not exist on Windows, and os.open there
        # opens in text mode.
        self.assertEqual(self.offenders(self.FLAGS), [])

    def test_every_privacy_decision_goes_through_osprivacy(self):
        # Windows keeps no mode bits and no uid: a check of either is a refusal there.
        self.assertEqual(self.offenders(self.MODES, self.MACOS_ONLY), [])

    def test_the_uid_is_read_only_on_the_macos_paths(self):
        self.assertEqual(self.offenders(re.compile(r"\bos\.getuid\(\)"), self.MACOS_ONLY), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
