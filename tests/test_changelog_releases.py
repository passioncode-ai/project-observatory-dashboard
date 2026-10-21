"""A released CHANGELOG section never changes after its tag, and new headings keep one format.

On 2026-10-20 merges after the v0.26.0 tag placed three pull requests' entries inside the
0.26.0 section; the 0.27.0 release moved them out by hand, comparing with
`git show v0.26.0:CHANGELOG.md`. That comparison is this test, for every tag in the checkout.

A checkout without tags (a shallow CI clone, an export) cannot compare anything: the test says
so as a skip, never as a pass. check.yml checks out with the whole history and its tags.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADING = re.compile(r"^## (\d+\.\d+\.\d+)\b")

#: Sections that changed after their tag before this test existed, frozen by the digest of their
#: CURRENT text: they may not change again. Each says what changed and why it stays.
EDITED_AFTER_TAG = {
    "0.3.1": ("a path in one entry corrected from `state/` to `store/`",
              "eb6ec0e5458948825c5f5718f0c51715156d77dffd4d241418fff4ab12da6b7e"),
    "0.19.2": ("the heading says the tag was never published and is included in 0.19.3",
               "7b6d2c255b6fdda6c029c3c16c91502e7a2ac2d65551185fb67f82dc2162253c"),
    "0.19.4": ("0.19.3's notes, which the version bump had left under 0.19.4, moved back to 0.19.3",
               "822f19d54bfb522293d5d2a0a055d1ab49ff373706f59300e0da4e864e342458"),
    "0.23.0": ("a meter entry added and the keyserver entry corrected: its code merged after the tag",
               "462ac514174c47e55698cf721169bae50707d7265877d8d94318f7b5ea2b785a"),
    "0.24.0": ("a private name removed from one entry (the public-release privacy gate)",
               "67a77d9c0d5c7877872f23f69b16ed6cdc0b508fd3b1343d248a945502cbef65"),
    "0.24.1": ("the OBS-52 collector rotation entry added after the tag",
               "c9bcfd8d239dc8e6ed9958f3bb1b53533b92d4a71e547fd1a6a250f920855514"),
}


def sections(text: str) -> dict[str, str]:
    """{version: the section's text, heading included, trailing blank lines dropped}."""
    out: dict[str, str] = {}
    current, lines = None, []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current:
                out[current] = "\n".join(lines).rstrip()
            m = HEADING.match(line)
            current, lines = (m.group(1) if m else None), [line]
        elif current:
            lines.append(line)
    if current:
        out[current] = "\n".join(lines).rstrip()
    return out


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def release_tags() -> list[str] | None:
    """The `vX.Y.Z` tags in this checkout, or None when Git cannot be asked."""
    try:
        run = subprocess.run(["git", "-C", str(ROOT), "tag", "--list", "v*"], capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    return [t for t in run.stdout.split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]


def at_tag(tag: str) -> str | None:
    run = subprocess.run(["git", "-C", str(ROOT), "show", f"{tag}:CHANGELOG.md"], capture_output=True, text=True,
                         timeout=60)
    return run.stdout if run.returncode == 0 else None


class ReleasedSectionsStayAsTagged(unittest.TestCase):
    def test_every_tagged_section_equals_its_text_at_the_tag(self):
        tags = release_tags()
        if not tags:
            self.skipTest("no release tags in this checkout (a shallow clone or an export): "
                          "released CHANGELOG sections NOT compared")
        now = sections((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        compared, drifted = 0, []
        for tag in tags:
            version = tag[1:]
            then = at_tag(tag)
            if then is None:
                continue  # v0.1.0 and v0.2.0 predate the file
            tagged = sections(then).get(version)
            if tagged is None:
                continue
            compared += 1
            current = now.get(version)
            if current is None:
                drifted.append(f"{version}: the section is gone")
            elif current != tagged:
                frozen = EDITED_AFTER_TAG.get(version)
                if frozen is None:
                    drifted.append(f"{version}: differs from `git show {tag}:CHANGELOG.md`; restore the tag's text "
                                   "and move later entries to `## Unreleased`")
                elif digest(current) != frozen[1]:
                    drifted.append(f"{version}: edited after the tag before ({frozen[0]}) and changed again since; "
                                   "a released section is history")
        self.assertGreater(compared, 0, "tags exist but no section was compared")
        self.assertEqual(drifted, [])

    def test_frozen_exceptions_are_real(self):
        """An exception for a section that equals its tag again is a stale entry: remove it."""
        tags = release_tags()
        if not tags:
            self.skipTest("no release tags in this checkout: the exceptions cannot be compared")
        now = sections((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        for version in EDITED_AFTER_TAG:
            if f"v{version}" not in tags:
                continue
            then = at_tag(f"v{version}")
            with self.subTest(version):
                self.assertIsNotNone(then)
                self.assertNotEqual(sections(then).get(version), now.get(version),
                                    f"{version} equals its tag: drop it from EDITED_AFTER_TAG")

    def test_the_comparison_catches_a_moved_entry(self):
        tagged = "# Changelog\n\n## 0.26.0 (2026-10-20)\n\n- one\n\n## 0.25.0 (2026-10-19)\n\n- old\n"
        later = "# Changelog\n\n## 0.27.0 (2026-10-21)\n\n## 0.26.0 (2026-10-20)\n\n- one\n- two\n\n## 0.25.0 (2026-10-19)\n\n- old\n"
        self.assertNotEqual(sections(tagged)["0.26.0"], sections(later)["0.26.0"])
        self.assertEqual(sections(tagged)["0.25.0"], sections(later)["0.25.0"])


class ChecksFetchTheTags(unittest.TestCase):
    def test_the_test_job_checks_out_history_and_tags(self):
        workflow = (ROOT / ".github/workflows/check.yml").read_text(encoding="utf-8")
        job = workflow.split("\n  windows:", 1)[0]
        self.assertTrue("fetch-depth: 0" in job, "the test job must check out the whole history")
        self.assertTrue("fetch-tags: true" in job, "the test job must fetch the release tags")


if __name__ == "__main__":
    unittest.main()
