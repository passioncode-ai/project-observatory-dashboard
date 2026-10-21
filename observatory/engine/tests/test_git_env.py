#!/usr/bin/env python3
"""A test's git never leaves a background `gc --auto` or maintenance behind.

WHY. On 2026-10-20 the release guard for v0.27.0 refused the tag: check.yml on e6e8673 failed in
`test_a_detached_worktree_joins_a_main_checkout_already_here` with "OSError: directory not
empty" while the test removed its temporary `.git`, because a `git gc --auto` that a git command
had detached was still writing into it. Git starts that work by itself after `commit`, `fetch`,
`merge`, `am` and `rebase`; it outlives the command and the test, and a cleanup racing it fails
on one runner in many. The fix for the restore itself (`machine_backup.clone`, #321) covered one
caller; this suite pins the class.

Every git a test runs takes its environment from `git_env.py`, which shuts out the operator's
system and global configuration and turns background maintenance off through
`GIT_CONFIG_COUNT`, appended after any entries a caller already set. The portable runner's
`clean_env` uses it, so a suite that inherits the runner's environment is covered too, and
check.yml's workflow-level `env:` carries the same entries for the root tests.
`tests/test_git_background_maintenance.py` (the repository's root tests) refuses a test that
builds a git environment by hand.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import git_env  # noqa: E402
import run_portable  # noqa: E402

GIT = shutil.which("git")


def git_version() -> tuple[int, ...]:
    if not GIT:
        return ()
    out = subprocess.run([GIT, "--version"], capture_output=True, text=True, timeout=30).stdout
    digits = [part for part in out.split() if part[:1].isdigit()]
    return tuple(int(x) for x in digits[0].split(".")[:2] if x.isdigit()) if digits else ()


def effective(env: dict[str, str], key: str, cwd: str) -> str | None:
    """The value git itself reads for `key` under `env`, or None when it reads none."""
    p = subprocess.run([GIT, "config", "--get", key], cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=30)
    return p.stdout.strip() if p.returncode == 0 else None


def entries(env: dict[str, str]) -> list[tuple[str, str]]:
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
            for i in range(int(env.get("GIT_CONFIG_COUNT", "0")))]


class Composition(unittest.TestCase):
    def test_an_empty_environment_gets_both_settings(self):
        env = git_env.quiet_maintenance({"PATH": "/usr/bin"})
        self.assertEqual(entries(env), [("gc.auto", "0"), ("maintenance.auto", "false")])
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_a_callers_own_entries_are_kept_and_ours_come_after(self):
        env = git_env.quiet_maintenance({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotePath",
                                         "GIT_CONFIG_VALUE_0": "false"})
        self.assertEqual(entries(env), [("core.quotePath", "false"), ("gc.auto", "0"),
                                        ("maintenance.auto", "false")])

    def test_composing_twice_adds_nothing(self):
        once = git_env.quiet_maintenance({})
        self.assertEqual(git_env.quiet_maintenance(once), once)

    def test_a_caller_that_turned_gc_back_on_is_overridden_after_it(self):
        # Later entries win in git, so ours must be the last word on these two keys.
        env = git_env.quiet_maintenance({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "GC.Auto",
                                         "GIT_CONFIG_VALUE_0": "50"})
        self.assertEqual(entries(env)[-2:], [("gc.auto", "0"), ("maintenance.auto", "false")])

    def test_a_count_git_would_refuse_is_refused_here_too(self):
        for bad in ("two", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                git_env.quiet_maintenance({"GIT_CONFIG_COUNT": bad})

    def test_an_outer_gits_options_cannot_turn_gc_back_on(self):
        # A git started by another git inherits its `-c` options here, ranked above GIT_CONFIG_COUNT.
        env = git_env.quiet_maintenance({"GIT_CONFIG_PARAMETERS": "'gc.auto'='50'"})
        self.assertNotIn("GIT_CONFIG_PARAMETERS", env)

    def test_the_input_is_not_changed(self):
        base = {"GIT_CONFIG_COUNT": "0"}
        git_env.quiet_maintenance(base)
        self.assertEqual(base, {"GIT_CONFIG_COUNT": "0"})


class Environment(unittest.TestCase):
    def test_the_default_shuts_out_system_and_global_configuration(self):
        env = git_env.git_env({"PATH": "/usr/bin"})
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertIn(("gc.auto", "0"), entries(env))

    def test_a_global_file_of_the_tests_own_is_kept(self):
        env = git_env.git_env({}, global_config="/synthetic/gitconfig")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/synthetic/gitconfig")

    def test_none_leaves_the_global_file_to_home(self):
        env = git_env.git_env({"GIT_CONFIG_GLOBAL": "/inherited"}, global_config=None)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/inherited")
        self.assertNotIn("GIT_CONFIG_GLOBAL", git_env.git_env({}, global_config=None))

    def test_drop_git_removes_every_inherited_git_variable_first(self):
        env = git_env.git_env({"GIT_DIR": "/elsewhere", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b",
                               "GIT_CONFIG_VALUE_0": "c", "HOME": "/h"}, drop_git=True, GIT_AUTHOR_NAME="Fixture")
        self.assertNotIn("GIT_DIR", env)
        self.assertEqual(entries(env), [("gc.auto", "0"), ("maintenance.auto", "false")])
        self.assertEqual((env["HOME"], env["GIT_AUTHOR_NAME"]), ("/h", "Fixture"))

    def test_the_default_base_is_the_process_environment(self):
        self.assertEqual(git_env.git_env()["PATH"], os.environ.get("PATH"))


@unittest.skipUnless(git_version() >= (2, 31), "needs git 2.31 or later, the first to read GIT_CONFIG_COUNT")
class WhatGitReads(unittest.TestCase):
    """Proven with git itself, not with the variables. A git older than 2.31 reads neither
    setting from the environment; these cases are then skipped and the receipt names them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_the_portable_runners_environment_turns_maintenance_off(self):
        (self.base / "suite").mkdir()
        env = run_portable.clean_env(self.base / "suite")
        self.assertEqual(effective(env, "gc.auto", self.tmp.name), "0")
        self.assertEqual(effective(env, "maintenance.auto", self.tmp.name), "false")

    def test_an_inherited_outer_option_does_not_win(self):
        env = git_env.git_env({"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name,
                               "GIT_CONFIG_PARAMETERS": "'gc.auto'='50'"})
        self.assertEqual(effective(env, "gc.auto", self.tmp.name), "0")

    def test_the_helpers_environment_turns_maintenance_off(self):
        env = git_env.git_env({"PATH": os.environ.get("PATH", ""), "HOME": self.tmp.name})
        self.assertEqual(effective(env, "gc.auto", self.tmp.name), "0")
        self.assertEqual(effective(env, "maintenance.auto", self.tmp.name), "false")


if __name__ == "__main__":
    unittest.main()
