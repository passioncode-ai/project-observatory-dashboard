#!/usr/bin/env python3
"""Asking git a question runs nothing else — proven with a planted configuration.

An audit ran the engine under a HOME whose `~/.gitconfig` named a program for
every hook git offers: fsmonitor, a hooks directory, `log.showSignature` with a
`gpg.program`, `diff.external`, a textconv driver, a clean/smudge filter through
the global attributes file, credential helpers, `core.sshCommand`, askpass and a
pager, plus config-declared hooks. One ordinary tick ran gpg, the fsmonitor, a
dozen hooks and the filter; the Stop hook ran them on every agent turn; and a
`cleanup --apply --include manual` archived a 0-byte patch and destroyed the
edit it was meant to keep.

This suite rebuilds that machine in a temporary HOME — synthetic repositories,
every planted program a script that leaves a marker — and drives the REAL
collectors, tools and hook functions over it. Each case asserts that no marker
appeared. The first case is the control: bare git over the same fixture DOES
leave markers, so an empty marker directory means the engine refused them, not
that the fixture was inert.

The legitimate behaviour the hardening must keep is asserted beside it: a remote
rewritten by `url.<base>.insteadOf` is still probed, a `.env` ignored only by the
global excludes file is still "ignored", and a commit still carries the author
the operator's own git would use.
"""
from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("", "collectors", "tools", "plugins", "tests"):
    sys.path.insert(0, str(ROOT / sub) if sub else str(ROOT))
import tmp as tmpdir  # noqa: E402
from git_env import git_env  # noqa: E402

HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout",
         "post-merge", "post-rewrite", "pre-push", "pre-auto-gc", "reference-transaction",
         "post-index-change", "fsmonitor-watchman")
FAKE_SIG = ("gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEFAKEFAKEFAKE=\n =FAKE\n"
            " -----END PGP SIGNATURE-----\n")


def bare(cwd, *args, env=None, check=True, text=True, input=None):
    """Fixture construction only: git with NO user configuration at all."""
    base = git_env(drop_git=True,
                   GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=cwd,
                          env={**base, **(env or {})}, capture_output=True, text=text,
                          input=input, check=check, timeout=120)


class Fixture:
    """A temporary machine: a hostile HOME, two repositories, a bare remote."""

    def __init__(self) -> None:
        self.d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-hostile-git-")).resolve()
        d = self.d
        self.marks, self.bin = d / "markers", d / "bin"
        self.home, self.projects = d / "home", d / "home" / "projects"
        for p in (self.marks, self.bin, self.projects, d / "hooks", d / "local-hooks", d / "xdg", d / "remotes"):
            p.mkdir(parents=True)
        self.alpha = self.projects / "alpha-web"
        self.beta = self.projects / "beta-api"
        self.beta_wt = self.projects / "beta-wt"
        self._build_repos()
        self._plant()

    # -- a program for every name, each leaving its own marker ---------------
    def program(self, name: str) -> str:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\necho \"$*\" >> '{self.marks}/{name}'\n"
                        "cat >/dev/null 2>&1\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def _seed(self, repo: pathlib.Path) -> None:
        bare(self.projects, "init", "-q", "-b", "main", str(repo))
        (repo / "README.md").write_text(f"# {repo.name}\n", encoding="utf-8")
        (repo / "notes.txt").write_text("one\n", encoding="utf-8")
        bare(repo, "add", "-A")
        bare(repo, "commit", "-qm", "init")
        # A signed commit at HEAD, so signature display has something to verify.
        tree = bare(repo, "write-tree").stdout.strip()
        parent = bare(repo, "rev-parse", "HEAD").stdout.strip()
        body = (f"tree {tree}\nparent {parent}\nauthor F <f@example.invalid> 1759400000 +0000\n"
                f"committer F <f@example.invalid> 1759400000 +0000\n{FAKE_SIG}\nsigned\n")
        sha = bare(repo, "hash-object", "-t", "commit", "-w", "--stdin", input=body).stdout.strip()
        bare(repo, "update-ref", "refs/heads/main", sha)

    def _build_repos(self) -> None:
        for repo in (self.alpha, self.beta):
            self._seed(repo)
        remote = self.d / "remotes" / "alpha-web.git"
        bare(self.d, "clone", "-q", "--bare", str(self.alpha), str(remote))
        # The recorded origin is an https address that only `insteadOf` turns
        # into this local bare repository: probing it offline proves the
        # operator's rewrite is still honoured.
        bare(self.alpha, "remote", "add", "origin", "https://example.invalid/alpha-web.git")
        head = bare(self.alpha, "rev-parse", "HEAD").stdout.strip()
        bare(self.alpha, "update-ref", "refs/remotes/origin/main", head)
        bare(self.beta, "remote", "add", "origin", "ssh://git@git.example.invalid/beta-api.git")
        bare(self.beta, "update-ref", "refs/remotes/origin/main", "HEAD")
        bare(self.beta, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        bare(self.beta, "branch", "merged-topic")
        bare(self.beta, "branch", "feature-x")
        bare(self.beta, "worktree", "add", "-q", str(self.beta_wt), "feature-x")
        for repo in (self.alpha, self.beta, self.beta_wt):
            with open(repo / "README.md", "a", encoding="utf-8") as fh:
                fh.write("uncommitted edit\n")
        (self.alpha / ".env").write_text("FIXTURE_TOKEN=FAKE-not-a-credential\n", encoding="utf-8")
        (self.alpha / "secrets").mkdir()
        (self.alpha / "secrets" / "token.txt").write_text("FAKE\n", encoding="utf-8")

    def _plant(self) -> None:
        d, p = self.d, self.program
        for name in HOOKS:
            for where, prefix in ((d / "hooks", "hook-"), (d / "local-hooks", "local-hook-")):
                (where / name).write_text(f"#!/bin/sh\nexec '{p(prefix + name)}' \"$@\"\n")
                (where / name).chmod(0o755)
        (d / "attrs").write_text("* filter=kc diff=kcdiff\n", encoding="utf-8")
        (d / "global-ignore").write_text(".env\n", encoding="utf-8")
        (self.home / ".gitconfig").write_text(f"""\
[user]
\tname = Fixture Author
\temail = author@example.invalid
\tsigningkey = FAKEKEY
[core]
\tfsmonitor = {p('fsmonitor')}
\thooksPath = {d / 'hooks'}
\tsshCommand = {p('ssh')}
\taskPass = {p('askpass')}
\tpager = {p('pager')}
\tattributesFile = {d / 'attrs'}
\texcludesFile = {d / 'global-ignore'}
[credential]
\thelper = {p('cred')}
[credential "https://example.invalid"]
\thelper = {p('cred-url')}
[log]
\tshowSignature = true
[gpg]
\tprogram = {p('gpg')}
[commit]
\tgpgsign = true
[tag]
\tgpgsign = true
[diff]
\texternal = {p('extdiff')}
[diff "kcdiff"]
\ttextconv = {p('textconv')}
[filter "kc"]
\tclean = {p('filter-clean')}
\tsmudge = {p('filter-smudge')}
[hook "kchook"]
\tcommand = {p('cfghook')}
\tevent = pre-commit
\tevent = reference-transaction
\tevent = post-index-change
\tevent = post-checkout
[gc]
\tauto = 1
[url "file://{d / 'remotes'}/"]
\tinsteadOf = https://example.invalid/
""", encoding="utf-8")
        # The repository's own configuration names programs too: dropping the
        # global file must not be the whole defence.
        local = {
            "core.fsmonitor": p("local-fsmonitor"), "core.hooksPath": str(d / "local-hooks"),
            "core.sshCommand": p("local-ssh"), "credential.helper": p("local-cred"),
            "log.showSignature": "true", "gpg.program": p("local-gpg"),
            "diff.external": p("local-extdiff"), "diff.lkd.textconv": p("local-textconv"),
            "filter.lkc.clean": p("local-filter-clean"), "filter.lkc.smudge": p("local-filter-smudge"),
            "filter.lkc.required": "true",
            "hook.lhook.command": p("local-cfghook"), "hook.lhook.event": "reference-transaction",
        }
        for key, value in local.items():
            bare(self.alpha, "config", key, value)
        bare(self.alpha, "config", "--add", "hook.lhook.event", "post-index-change")
        bare(self.alpha, "config", "--add", "hook.lhook.event", "pre-commit")
        (self.alpha / ".git" / "info" / "attributes").write_text("* filter=lkc diff=lkd\n", encoding="utf-8")
        # A transport helper that must never start.
        shim = self.bin / "git-remote-gcrypt"
        shim.write_text(f"#!/bin/sh\necho \"$*\" >> '{self.marks}/gcrypt'\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)
        # Make the dirty files stat-dirty for certain, so filters would be asked.
        for repo in (self.alpha, self.beta, self.beta_wt):
            os.utime(repo / "README.md", (time.time() + 5, time.time() + 5))

    def markers(self) -> dict[str, str]:
        return {m.name: m.read_text(encoding="utf-8", errors="replace")[:200]
                for m in sorted(self.marks.iterdir())}

    def clear(self) -> None:
        for m in self.marks.iterdir():
            m.unlink()

    def environ(self) -> dict[str, str]:
        # The hostile global file stays: it is read from this HOME (global_config=None).
        return git_env(drop_git=True, global_config=None, HOME=str(self.home), XDG_CONFIG_HOME=str(self.d / "xdg"),
                       PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")


FIX: Fixture | None = None


def fixture() -> Fixture:
    global FIX
    if FIX is None:
        FIX = Fixture()
    return FIX


class HostileBase(unittest.TestCase):
    """Every case runs in the hostile HOME and must leave no marker."""

    def setUp(self) -> None:
        self.f = fixture()
        self.f.clear()
        self.saved = dict(os.environ)
        hostile = self.f.environ()
        os.environ.clear()
        os.environ.update(hostile)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.saved)

    def assertNothingRan(self, what: str) -> None:
        self.assertEqual(self.f.markers(), {}, f"{what} ran a planted program")


class Control(HostileBase):
    def test_the_fixture_is_live_bare_git_runs_the_planted_programs(self) -> None:
        # Bare git under the hostile HOME: its global file stays (global_config=None).
        env = git_env(global_config=None)
        subprocess.run(["git", "-C", str(self.f.alpha), "log", "-1", "--format=%cI"],
                       env=env, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.f.beta), "status", "--porcelain"],
                       env=env, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        got = self.f.markers()
        self.assertIn("local-gpg", got, "signature display did not run the repository's gpg")
        self.assertTrue({"fsmonitor", "filter-clean"} & set(got),
                        f"status ran neither fsmonitor nor the filter: {sorted(got)}")


class TheDoor(HostileBase):
    def test_reads_and_writes_run_nothing(self) -> None:
        import safe_git
        for repo in (self.f.alpha, self.f.beta, self.f.beta_wt):
            status = safe_git.run(["status", "--porcelain"], repo=repo, timeout=60)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("README.md", status.stdout)
            diff = safe_git.run(["diff", "HEAD"], repo=repo, timeout=60)
            self.assertIn("+uncommitted edit", diff.stdout)
            self.assertEqual(safe_git.run(["log", "-1", "--format=%s"], repo=repo, timeout=60).stdout.strip(),
                             "signed")
            safe_git.run(["ls-files"], repo=repo, timeout=60)
        made = safe_git.run(["branch", "door-made"], repo=self.f.beta, write=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        gone = safe_git.run(["branch", "-D", "door-made"], repo=self.f.beta, write=True, timeout=60)
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertNothingRan("safe_git")

    def test_the_global_excludes_file_still_decides_ignored(self) -> None:
        import safe_git
        p = safe_git.run(["check-ignore", "-q", ".env"], repo=self.f.alpha, timeout=60)
        self.assertEqual(p.returncode, 0, "a .env ignored by the global excludes file read as not ignored")
        self.assertNothingRan("check-ignore")

    def test_the_forwarded_file_holds_values_only_and_is_private(self) -> None:
        import safe_git
        path = safe_git.forward_file()
        self.assertNotEqual(path, os.devnull)
        text = pathlib.Path(path).read_text(encoding="utf-8")
        self.assertIn("excludesfile", text)
        self.assertIn("insteadof", text)
        for program_key in ("helper", "fsmonitor", "hookspath", "program", "external", "clean",
                            "sshcommand", "askpass", "pager", "showsignature", "gpgsign", "command"):
            self.assertNotIn(program_key, text.lower(), program_key)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)

    def test_render_round_trips_awkward_values(self) -> None:
        import safe_git
        entries = [("safe.directory", "/srv/example-ws/with \"quote\" and \\slash"),
                   ("safe.directory", "/srv/example-ws/tab\there"),
                   ("url.https://example.invalid/a \"b\"/.insteadof", "git@example.invalid:"),
                   ("core.autocrlf", None)]
        with tempfile.TemporaryDirectory() as t:
            f = pathlib.Path(t) / "c"
            f.write_text(safe_git.render(entries), encoding="utf-8")
            back = bare(t, "config", "--file", str(f), "--null", "--get-regexp", ".", text=False).stdout
        self.assertEqual(safe_git.parse_null(back), entries)

    def test_an_unreadable_configuration_refuses_rather_than_runs(self) -> None:
        import safe_git
        with tempfile.TemporaryDirectory() as t:
            bare(t, "init", "-q")
            (pathlib.Path(t) / ".git" / "config").write_text("[broken\n", encoding="utf-8")
            r = safe_git.run(["status"], repo=t, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(r.stderr)


class Collectors(HostileBase):
    def test_scan_filesystem_the_ticks_first_step(self) -> None:
        out = self.f.d / "local.json"
        env = {**os.environ, "OBSERVATORY_DATA": str(self.f.projects)}
        p = subprocess.run([sys.executable, "collectors/scan_filesystem.py", str(out)], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, (p.stdout + p.stderr)[-600:])
        self.assertNothingRan("scan_filesystem")

    def test_scan_events_scan_env_hygiene_and_secret_files(self) -> None:
        import scan_events, scan_env, scan_git_hygiene as hygiene, credentials_registry
        log, why = scan_events.git(self.f.alpha, "log", "-5", "--no-merges", "--format=%H%x1f%s%x1e")
        self.assertIsNone(why)
        self.assertIn("signed", log)
        states, degraded = scan_env.git_states([self.f.alpha / ".env"], self.f.projects)
        self.assertEqual(states[str(self.f.alpha / ".env")], "ignored", "the global excludes file was dropped")
        self.assertEqual(degraded, [])
        wts = hygiene.worktrees(str(self.f.beta), set(), time.time())
        self.assertEqual([w["state"] for w in wts], ["dirty"])
        names = {b["name"]: b["class"] for b in hygiene.branches(str(self.f.beta), "main", {"main", "feature-x"}, time.time())}
        self.assertEqual(names.get("merged-topic"), "merged")
        rows = credentials_registry.from_project_secrets(self.f.projects)
        self.assertTrue(rows)
        self.assertNothingRan("collectors")

    def test_plugins_and_identity(self) -> None:
        import release_cadence, git_branches, identity_map
        self.assertIsNone(release_cadence.git(self.f.alpha, "for-each-ref", "refs/tags")[1])
        self.assertGreaterEqual(git_branches.branches(self.f.beta), 3)
        self.assertTrue(identity_map.root_commit(str(self.f.alpha)))
        self.assertNothingRan("plugins")

    def test_remote_probe_honours_insteadof_and_refuses_helpers(self) -> None:
        import scan_remotes
        _, got = scan_remotes.probe({"folder": "alpha-web", "path": str(self.f.alpha),
                                     "remote": "https://example.invalid/alpha-web.git", "branch": "main"})
        self.assertTrue(got.get("reachable"), got)
        self.assertEqual(got.get("sync"), "current", got)
        _, got = scan_remotes.probe({"folder": "alpha-web", "path": str(self.f.alpha),
                                     "remote": "gcrypt::rsync://example.invalid/repo", "branch": "main"})
        self.assertFalse(got.get("reachable"), got)
        self.assertIn("not allowed", got.get("reason", ""))
        self.assertNothingRan("scan_remotes")


class HookAndTools(HostileBase):
    def test_the_stop_hook_and_the_session_start_hook(self) -> None:
        import record_turn, session_start
        facts = record_turn.diff_facts(self.f.alpha)
        self.assertIn("README.md", facts["files"])
        self.assertEqual(facts["insertions"], 1)
        self.assertEqual(session_start.git_top(self.f.alpha), self.f.alpha)
        # `get-url` applies the operator's `insteadOf`, as their own git does.
        self.assertTrue(session_start.remote_of(self.f.alpha).endswith("/remotes/alpha-web.git"))
        self.assertNothingRan("the companion hooks")

    def test_corroborate_and_service_identity(self) -> None:
        import corroborate, service_identity
        head = bare(self.f.beta, "rev-parse", "HEAD").stdout.strip()
        code, out = corroborate.git(self.f.beta, "branch", "-a", "--contains", head)
        self.assertEqual(code, 0)
        self.assertIn("main", out)
        self.assertTrue(service_identity._git(self.f.beta, "rev-parse", "--short=12", "HEAD"))
        self.assertNothingRan("corroborate / service_identity")

    def test_commits_carry_the_operators_author_and_run_no_hook_or_signer(self) -> None:
        import commit_registry, commit_projection
        for module, name in ((commit_registry, "registry"), (commit_projection, "inventory")):
            repo = self.f.d / f"commit-{name}"
            bare(self.f.d, "init", "-q", "-b", "main", str(repo))
            bare(repo, "config", "core.hooksPath", str(self.f.d / "local-hooks"))
            bare(repo, "config", "hook.lhook.command", self.f.program("local-cfghook"))
            bare(repo, "config", "hook.lhook.event", "pre-commit")
            (repo / name).mkdir()
            (repo / name / "data.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(module.git("add", "--", name, cwd=repo)[0], 0)
            code, out = module.git("commit", "-q", "-m", "generated", cwd=repo)
            self.assertEqual(code, 0, out)
            author = bare(repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
            self.assertEqual(author, "Fixture Author <author@example.invalid>")
        self.assertNothingRan("commit_registry / commit_projection")

    def test_cleanup_archives_and_removes_without_running_anything(self) -> None:
        import cleanup
        # Holder behavior has separate synthetic negative/positive coverage in
        # test_disk_guard; this suite only exercises the hostile Git boundary.
        for name in ("busy_now", "fresh_busy"):
            probe = patch.object(cleanup, name, return_value=False)
            probe.start()
            self.addCleanup(probe.stop)
        archive = self.f.d / "archive"
        # Its own branch and worktree: the other cases read the shared ones.
        wt = self.f.projects / "beta-cleanup-wt"
        bare(self.f.beta, "branch", "merged-cleanup")
        bare(self.f.beta, "branch", "cleanup-topic")
        bare(self.f.beta, "worktree", "add", "-q", str(wt), "cleanup-topic")
        with open(wt / "README.md", "a", encoding="utf-8") as fh:
            fh.write("uncommitted edit\n")
        os.utime(wt / "README.md", (time.time() + 5, time.time() + 5))
        self.f.clear()
        merged = cleanup.act({"class": "branch-merged", "tier": "auto", "repo": str(self.f.beta),
                              "target": "merged-cleanup", "default_branch": "main",
                              "repository": "repository:example-org/beta-api"}, archive)
        self.assertEqual(merged["result"], "removed", merged)
        rec = cleanup.act({"class": "worktree-dirty", "tier": "manual", "repo": str(self.f.beta),
                           "target": str(wt), "branch": "cleanup-topic",
                           "repository": "repository:example-org/beta-api"}, archive)
        self.assertEqual(rec["result"], "removed", rec)
        archived_patch = (pathlib.Path(rec["archive"]) / "tracked.patch").read_text(encoding="utf-8")
        self.assertIn("+uncommitted edit", archived_patch)
        self.assertNothingRan("cleanup")


class CleanupNeverLosesWork(unittest.TestCase):
    """`cleanup --apply --include manual` removes a dirty worktree with
    `--force --force`, so its archived patch is the only copy of the edit. It
    must be proven to apply before anything is removed (KC-R3-4: a 0-byte
    patch, "1 removed, 0 failed", and the edit gone)."""

    def setUp(self) -> None:
        import cleanup
        self.cleanup = cleanup
        # Holder behavior has separate synthetic negative/positive coverage in
        # test_disk_guard; this suite only exercises the hostile Git boundary.
        for name in ("busy_now", "fresh_busy"):
            probe = patch.object(cleanup, name, return_value=False)
            probe.start()
            self.addCleanup(probe.stop)
        self.d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-cleanup-loss-")).resolve()
        self.repo, self.wt = self.d / "alpha-web", self.d / "alpha-wt"
        bare(self.d, "init", "-q", "-b", "main", str(self.repo))
        (self.repo / "old.txt").write_text("".join(f"line {i}\n" for i in range(20)), encoding="utf-8")
        bare(self.repo, "add", "-A")
        bare(self.repo, "commit", "-qm", "init")
        bare(self.repo, "branch", "topic")
        bare(self.repo, "worktree", "add", "-q", str(self.wt), "topic")
        with open(self.wt / "old.txt", "a", encoding="utf-8") as fh:
            fh.write("the only copy of this edit\n")

    def act(self) -> dict:
        return self.cleanup.act({"class": "worktree-dirty", "tier": "manual", "repo": str(self.repo),
                                 "target": str(self.wt), "branch": "topic",
                                 "repository": "repository:example-org/alpha-web"}, self.d / "archive")

    def assertKept(self, rec: dict) -> None:
        self.assertEqual(rec["result"], "failed", rec)
        self.assertIn("nothing removed", rec["reason"])
        self.assertIn("the only copy of this edit", (self.wt / "old.txt").read_text(encoding="utf-8"))

    def assertRestorable(self, rec: dict) -> None:
        """The archived patch, applied to a fresh checkout of HEAD, gives back the edit."""
        self.assertEqual(rec["result"], "removed", rec)
        self.assertFalse(self.wt.exists())
        patch = pathlib.Path(rec["archive"]) / "tracked.patch"
        fresh = self.d / "restore"
        bare(self.d, "clone", "-q", str(self.repo), str(fresh))
        bare(fresh, "apply", str(patch))
        self.assertIn("the only copy of this edit", (fresh / "old.txt").read_text(encoding="utf-8"))

    def test_an_external_diff_driver_cannot_become_the_archive(self) -> None:
        # A difftastic-like driver: prints its own display text and exits 0.
        driver = self.d / "display-diff"
        driver.write_text("#!/bin/sh\necho \"display: $1 changed\"\nexit 0\n", encoding="utf-8")
        driver.chmod(0o755)
        bare(self.repo, "config", "diff.external", str(driver))
        self.assertRestorable(self.act())

    def test_a_diff_that_fails_removes_nothing(self) -> None:
        # The committed blob of the edited file is gone: status still answers
        # (the worktree is hashed, not compared), `git diff` cannot.
        blob = bare(self.repo, "rev-parse", "HEAD:old.txt").stdout.strip()
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        self.assertKept(self.act())

    def test_a_patch_that_does_not_apply_removes_nothing(self) -> None:
        real = self.cleanup.git

        def corrupting(repo, *args, **kw):
            p = real(repo, *args, **kw)
            if args[:2] == ("diff", "HEAD"):
                p.stdout = b"diff --git a/old.txt b/old.txt\n--- a/old.txt\n+++ b/old.txt\n@@ -1 +1 @@\n-no such line\n+x\n"
            return p
        self.cleanup.git = corrupting
        try:
            self.assertKept(self.act())
        finally:
            self.cleanup.git = real

    def test_an_empty_patch_for_a_dirty_tree_removes_nothing(self) -> None:
        real = self.cleanup.git

        def emptying(repo, *args, **kw):
            p = real(repo, *args, **kw)
            if args[:2] == ("diff", "HEAD"):
                p.stdout = b""
            return p
        self.cleanup.git = emptying
        try:
            self.assertKept(self.act())
        finally:
            self.cleanup.git = real

    def test_tracked_edit_during_archive_refuses_removal(self) -> None:
        original = self.cleanup.export_tracked
        def changed(target, status):
            result = original(target, status)
            (self.wt / "old.txt").write_text("the only copy of this edit\nnewer edit\n")
            return result
        with patch.object(self.cleanup, "export_tracked", side_effect=changed):
            self.assertKept(self.act())
        self.assertIn("newer edit", (self.wt / "old.txt").read_text())

    def test_untracked_edit_after_tar_write_refuses_removal(self) -> None:
        import tarfile
        untracked = self.wt / "notes.txt"
        untracked.write_text("first note\n")
        original = tarfile.TarFile.add
        def changed(tar, name, *args, **kwargs):
            result = original(tar, name, *args, **kwargs)
            untracked.write_text("newer only copy\n")
            return result
        with patch.object(tarfile.TarFile, "add", changed):
            self.assertKept(self.act())
        self.assertEqual(untracked.read_text(), "newer only copy\n")

    def test_nested_untracked_repository_is_preserved(self) -> None:
        nested = self.wt / "nested"
        bare(self.wt, "init", "-q", str(nested))
        (nested / "only.txt").write_text("uncommitted nested work\n")
        self.assertKept(self.act())
        self.assertTrue((nested / ".git").is_dir())
        self.assertEqual((nested / "only.txt").read_text(), "uncommitted nested work\n")

    def test_manual_regular_untracked_content_is_verified_and_restorable(self) -> None:
        import hashlib, json, tarfile
        content = b"untracked binary\x00payload\n"
        (self.wt / "notes.bin").write_bytes(content)
        rec = self.act()
        self.assertRestorable(rec)
        dest = pathlib.Path(rec["archive"])
        manifest = json.loads((dest / "untracked-manifest.json").read_text())
        self.assertEqual(manifest["notes.bin"]["sha256"], hashlib.sha256(content).hexdigest())
        with tarfile.open(dest / "untracked.tgz", "r:gz") as tar:
            self.assertEqual(tar.extractfile("notes.bin").read(), content)

    def test_manual_archive_budget_exhaustion_preserves_work(self) -> None:
        with patch.object(self.cleanup, "ARCHIVE_SECONDS", 0, create=True):
            self.assertKept(self.act())

    def test_archive_read_failure_preserves_work(self) -> None:
        import tarfile
        (self.wt / "notes.txt").write_text("only note\n")
        original = tarfile.open
        def unreadable(name, mode="r", *args, **kwargs):
            if mode.startswith("r"):
                raise OSError("synthetic archive read error")
            return original(name, mode, *args, **kwargs)
        with patch.object(tarfile, "open", side_effect=unreadable):
            self.assertKept(self.act())

    def test_untracked_special_file_is_preserved(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO requires POSIX")
        fifo = self.wt / "queue"
        os.mkfifo(fifo)
        self.assertKept(self.act())
        self.assertTrue(fifo.exists())

    def test_legacy_artifact_plan_cannot_delete_newly_tracked_output(self) -> None:
        output = self.repo / "dist"
        output.mkdir()
        (output / "only.txt").write_text("new source, not disposable\n")
        bare(self.repo, "add", "dist")
        result = self.cleanup.act({"class": "build-artifacts", "tier": "auto", "target": str(output)},
                                  self.d / "archive")
        self.assertEqual(result["result"], "skipped")
        self.assertIn("full disk-guard", result["reason"])
        self.assertEqual((output / "only.txt").read_text(), "new source, not disposable\n")

    def test_head_change_during_archive_refuses_removal(self) -> None:
        original = self.cleanup.export_tracked
        def changed(target, status):
            result = original(target, status)
            bare(self.wt, "add", "old.txt")
            bare(self.wt, "commit", "-qm", "concurrent work")
            return result
        with patch.object(self.cleanup, "export_tracked", side_effect=changed):
            self.assertKept(self.act())

    def test_added_untracked_file_after_tar_write_refuses_removal(self) -> None:
        import tarfile
        (self.wt / "notes.txt").write_text("original note\n")
        original = tarfile.TarFile.add
        def changed(tar, name, *args, **kwargs):
            result = original(tar, name, *args, **kwargs)
            (self.wt / "new.txt").write_text("new only copy\n")
            return result
        with patch.object(tarfile.TarFile, "add", changed):
            self.assertKept(self.act())
        self.assertEqual((self.wt / "new.txt").read_text(), "new only copy\n")

    def test_untracked_symlink_and_bare_repository_are_preserved(self) -> None:
        link = self.wt / "link"
        link.symlink_to("old.txt")
        self.assertKept(self.act())
        self.assertTrue(link.is_symlink())
        link.unlink()
        nested = self.wt / "nested.git"
        bare(self.wt, "init", "--bare", "-q", str(nested))
        self.assertKept(self.act())
        self.assertTrue((nested / "HEAD").is_file())

    def test_hashing_reads_bounded_chunks_and_refuses_excess(self) -> None:
        import io
        calls = []
        class Source(io.BytesIO):
            def read(self, count=-1):
                calls.append(count)
                return super().read(count)
        data = b"x" * (2 * 1024 * 1024 + 7)
        size, _ = self.cleanup.stream_digest(Source(data), time.monotonic() + 30, len(data))
        self.assertEqual(size, len(data))
        self.assertTrue(all(0 < n <= 1024 * 1024 for n in calls))
        with self.assertRaises(self.cleanup.ArchiveRefused):
            self.cleanup.stream_digest(Source(data), time.monotonic() + 30, 10)

    def test_untracked_read_error_refuses_removal(self) -> None:
        note = self.wt / "notes.txt"
        note.write_text("only note\n")
        original = os.open
        def denied(path, *args, **kwargs):
            if pathlib.Path(path) == note:
                raise PermissionError("synthetic unreadable file")
            return original(path, *args, **kwargs)
        with patch.object(self.cleanup.os, "open", side_effect=denied):
            rec = self.act()
        self.assertKept(rec)
        self.assertIn("archive", rec)  # the partial recovery directory stays named
        self.assertEqual(note.read_text(), "only note\n")

    def test_distinct_staged_and_working_edits_are_both_archived(self) -> None:
        (self.wt / "old.txt").write_text("staged only copy\n")
        bare(self.wt, "add", "old.txt")
        (self.wt / "old.txt").write_text("the only copy of this edit\nworking copy\n")
        rec = self.act()
        self.assertRestorable(rec)
        staged = pathlib.Path(rec["archive"]) / "staged.patch"
        self.assertIn("+staged only copy", staged.read_text())
        restored = self.d / "restore-index"
        bare(self.d, "clone", "-q", str(self.repo), str(restored))
        bare(restored, "apply", "--cached", str(staged))
        self.assertEqual(bare(restored, "show", ":old.txt").stdout, "staged only copy\n")

    def test_staged_edit_during_archive_refuses_removal(self) -> None:
        path = self.wt / "old.txt"
        path.write_text("first staged copy\n")
        bare(self.wt, "add", "old.txt")
        path.write_text("the only copy of this edit\nworking copy\n")
        original = self.cleanup.archive_manual
        def revise(*args, **kwargs):
            result = original(*args, **kwargs)
            working = path.read_bytes()
            path.write_text("new staged only copy\n")
            bare(self.wt, "add", "old.txt")
            path.write_bytes(working)
            return result
        with patch.object(self.cleanup, "archive_manual", side_effect=revise):
            rec = self.act()
        self.assertKept(rec)
        self.assertIn("index changed", rec["reason"])
        self.assertEqual(bare(self.wt, "show", ":old.txt").stdout, "new staged only copy\n")

    def _worktree_action(self, cls="worktree-dirty", branch="topic") -> dict:
        return self.cleanup.act({"class": cls, "tier": "manual" if cls.endswith("dirty") else "auto",
                                 "repo": str(self.repo), "target": str(self.wt), "branch": branch}, self.d / "archive")

    def _hidden_index_case(self, flag, cls):
        bare(self.wt, "update-index", flag, "old.txt")
        (self.wt / "old.txt").write_text("hidden only copy\n")
        if cls.endswith("dirty"):
            (self.wt / "visible.txt").write_text("visible edit\n")
        rec = self._worktree_action(cls)
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual((self.wt / "old.txt").read_text(), "hidden only copy\n")
        self.assertIn("index", rec["reason"])

    def test_assume_unchanged_refuses_manual_and_auto_removal(self):
        for cls in ("worktree-dirty", "worktree-clean"):
            with self.subTest(cls=cls):
                self.setUp()
                self._hidden_index_case("--assume-unchanged", cls)

    def test_skip_worktree_refuses_manual_and_auto_removal(self):
        for cls in ("worktree-dirty", "worktree-clean"):
            with self.subTest(cls=cls):
                self.setUp()
                self._hidden_index_case("--skip-worktree", cls)

    def test_index_flag_appearing_during_archive_refuses_removal(self):
        original = self.cleanup.archive_manual
        def revise(*args, **kwargs):
            result = original(*args, **kwargs)
            bare(self.wt, "update-index", "--assume-unchanged", "old.txt")
            return result
        with patch.object(self.cleanup, "archive_manual", side_effect=revise):
            rec = self.act()
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertTrue(self.wt.exists())

    def test_unreadable_index_flags_refuse_removal(self):
        original = self.cleanup.git
        def unreadable(repo, *args, **kwargs):
            if args[:2] == ("ls-files", "-v"):
                return subprocess.CompletedProcess(args, 1, "", "synthetic read failure")
            return original(repo, *args, **kwargs)
        with patch.object(self.cleanup, "git", side_effect=unreadable):
            rec = self.act()
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertTrue(self.wt.exists())

    def _detached_unique_head(self):
        bare(self.wt, "checkout", "--detach", "-q")
        bare(self.wt, "add", "old.txt")
        bare(self.wt, "commit", "-qm", "unique detached change")
        head = bare(self.wt, "rev-parse", "HEAD").stdout.strip()
        with (self.wt / "old.txt").open("a") as f:
            f.write("new working edit\n")
        return head, "archive/wt-" + head[:8]

    def test_detached_archive_ref_conflict_retains_unique_head(self):
        head, name = self._detached_unique_head()
        bare(self.repo, "branch", name, "main")
        rec = self._worktree_action(branch=None)
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.wt, "rev-parse", "HEAD").stdout.strip(), head)

    def test_detached_archive_ref_creation_failure_retains_worktree(self):
        head, name = self._detached_unique_head()
        original = self.cleanup.git
        def refuse(repo, *args, **kwargs):
            if args[:2] == ("branch", name):
                return subprocess.CompletedProcess(args, 1, "", "synthetic ref failure")
            return original(repo, *args, **kwargs)
        with patch.object(self.cleanup, "git", side_effect=refuse):
            rec = self._worktree_action(branch=None)
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertTrue(self.wt.exists())

    def test_matching_detached_archive_ref_permits_removal(self):
        head, name = self._detached_unique_head()
        bare(self.repo, "branch", name, head)
        rec = self._worktree_action(branch=None)
        self.assertEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "rev-parse", name).stdout.strip(), head)

    def test_detached_head_drift_after_ref_creation_refuses_removal(self):
        head, name = self._detached_unique_head()
        original = self.cleanup.git
        def revise(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("branch", name):
                bare(self.wt, "add", "old.txt")
                bare(self.wt, "commit", "-qm", "new detached head")
            return result
        with patch.object(self.cleanup, "git", side_effect=revise):
            rec = self._worktree_action(branch=None)
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertTrue(self.wt.exists())

    def test_new_detached_archive_ref_retains_unique_head(self):
        head, name = self._detached_unique_head()
        rec = self._worktree_action(branch="stale-survey-branch")
        self.assertEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "rev-parse", name).stdout.strip(), head)

    def _unique_branch(self):
        bare(self.wt, "add", "old.txt")
        bare(self.wt, "commit", "-qm", "unique branch change")
        head = bare(self.wt, "rev-parse", "HEAD").stdout.strip()
        bare(self.wt, "checkout", "--detach", "-q")
        return head

    def _branch_action(self, cls="branch-unique"):
        return self.cleanup.act({"class": cls, "tier": "manual", "repo": str(self.repo),
                                 "target": "topic", "default_branch": "main",
                                 "repository": "repository:example-org/alpha-web"}, self.d / "archive")

    def _advance_topic(self):
        head = bare(self.repo, "rev-parse", "topic").stdout.strip()
        tree = bare(self.repo, "rev-parse", "topic^{tree}").stdout.strip()
        new = bare(self.repo, "commit-tree", tree, "-p", head, input="new only commit\n").stdout.strip()
        bare(self.repo, "update-ref", "refs/heads/topic", new)
        return new

    def test_unique_branch_drift_during_bundle_verification_is_retained(self):
        self._unique_branch()
        original = self.cleanup.git
        advanced = []
        def drift(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("bundle", "verify"):
                advanced.append(self._advance_topic())
            return result
        with patch.object(self.cleanup, "git", side_effect=drift):
            rec = self._branch_action()
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "rev-parse", "topic").stdout.strip(), advanced[0])

    def test_wrong_bundle_tip_refuses_branch_removal(self):
        self._unique_branch()
        original = self.cleanup.git
        def wrong(repo, *args, **kwargs):
            if args[:2] == ("bundle", "list-heads"):
                return subprocess.CompletedProcess(args, 0, "0" * 40 + " refs/heads/topic\n", "")
            return original(repo, *args, **kwargs)
        with patch.object(self.cleanup, "git", side_effect=wrong):
            rec = self._branch_action()
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/topic", check=False).returncode, 0)

    def test_unique_branch_verified_tip_is_archived_and_removed(self):
        head = self._unique_branch()
        rec = self._branch_action()
        self.assertEqual(rec["result"], "removed", rec)
        self.assertIn(head + " refs/heads/topic", bare(self.repo, "bundle", "list-heads", rec["archive"]).stdout)

    def test_auto_branch_drift_at_delete_is_refused_by_cas(self):
        bare(self.wt, "checkout", "--detach", "-q")
        original = self.cleanup.git
        advanced = []
        def drift(repo, *args, **kwargs):
            if args[:2] in (("branch", "-D"), ("update-ref", "-d")):
                advanced.append(self._advance_topic())
            return original(repo, *args, **kwargs)
        with patch.object(self.cleanup, "git", side_effect=drift):
            rec = self._branch_action("branch-merged")
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "rev-parse", "topic").stdout.strip(), advanced[0])

    def test_index_flag_appearing_after_auto_status_refuses_removal(self):
        bare(self.wt, "checkout", "--", "old.txt")
        original = self.cleanup.ignored_keepers
        def revise(*args, **kwargs):
            result = original(*args, **kwargs)
            bare(self.wt, "update-index", "--skip-worktree", "old.txt")
            (self.wt / "old.txt").write_text("hidden late copy\n")
            return result
        with patch.object(self.cleanup, "ignored_keepers", side_effect=revise):
            rec = self._worktree_action("worktree-clean")
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual((self.wt / "old.txt").read_text(), "hidden late copy\n")

    def test_unique_branch_checked_out_is_never_deleted(self):
        rec = self._branch_action()
        self.assertEqual(rec["result"], "skipped", rec)
        self.assertIn("checked out", rec["reason"])

    def test_unknown_branch_worktree_listing_refuses_deletion(self):
        self._unique_branch()
        original = self.cleanup.git
        def unreadable(repo, *args, **kwargs):
            if args[:2] == ("worktree", "list"):
                return subprocess.CompletedProcess(args, 1, "", "unreadable")
            return original(repo, *args, **kwargs)
        with patch.object(self.cleanup, "git", side_effect=unreadable):
            rec = self._branch_action()
        self.assertEqual(rec["result"], "skipped", rec)
        self.assertIn("could not be read", rec["reason"])

    def test_symbolic_branch_alias_cannot_delete_checked_out_referent(self):
        bare(self.wt, "checkout", "--detach", "-q")
        head = bare(self.repo, "rev-parse", "main").stdout.strip()
        bare(self.repo, "symbolic-ref", "refs/heads/topic", "refs/heads/main")
        rec = self._branch_action("branch-merged")
        self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "rev-parse", "--verify", "main").stdout.strip(), head)
        self.assertEqual(bare(self.repo, "symbolic-ref", "refs/heads/topic").stdout.strip(), "refs/heads/main")

    def test_late_symbolic_branch_alias_never_deletes_referent(self):
        bare(self.wt, "checkout", "--detach", "-q")
        head = bare(self.repo, "rev-parse", "main").stdout.strip()
        original = self.cleanup.git
        changed = []
        def alias(repo, *args, **kwargs):
            if args[:2] == ("update-ref", "-d"):
                bare(self.repo, "symbolic-ref", "refs/heads/topic", "refs/heads/main")
                changed.append(True)
            return original(repo, *args, **kwargs)
        with patch.object(self.cleanup, "git", side_effect=alias):
            self._branch_action("branch-merged")
        self.assertTrue(changed)
        self.assertEqual(bare(self.repo, "rev-parse", "--verify", "main").stdout.strip(), head)

    def test_invalid_branch_names_and_classes_do_not_reach_deletion(self):
        for name, cls in [("HEAD", "branch-merged"), ("@{-1}", "branch-merged"),
                          ("-D", "branch-unique"), ("bad..ref", "branch-unique"),
                          ("topic", "branch-unsupported")]:
            with self.subTest(name=name, cls=cls):
                rec = self.cleanup.act({"class": cls, "tier": "auto", "repo": str(self.repo),
                                        "target": name, "default_branch": "main"}, self.d / "archive")
                self.assertNotEqual(rec["result"], "removed", rec)
        self.assertEqual(bare(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/main", check=False).returncode, 0)


class RemoteReason(unittest.TestCase):
    """An unreachable remote is reported with git's CAUSE, not its boilerplate.

    For an SSH failure git prints the cause first and its generic advice last
    ("… and the repository exists."); keeping the last line put that advice in
    front of the operator as the reason (NU-R3-2)."""

    def setUp(self) -> None:
        self.d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-remote-reason-")).resolve()
        shim = self.d / "ssh"
        # Stands in for ssh, offline: the message ssh prints for a name that
        # does not resolve, and ssh's exit code.
        shim.write_text("#!/bin/sh\necho 'ssh: Could not resolve hostname git.example.invalid: "
                        "nodename nor servname provided, or not known' >&2\nexit 255\n", encoding="utf-8")
        shim.chmod(0o755)
        self.saved = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.d}{os.pathsep}{self.saved}"
        self.repo = self.d / "beta-api"
        bare(self.d, "init", "-q", "-b", "main", str(self.repo))

    def tearDown(self) -> None:
        os.environ["PATH"] = self.saved

    def test_the_first_line_that_names_the_cause_is_kept(self) -> None:
        import scan_remotes
        _, got = scan_remotes.probe({"folder": "beta-api", "path": str(self.repo),
                                     "remote": "ssh://git@git.example.invalid/beta-api.git", "branch": "main"})
        self.assertFalse(got["reachable"], got)
        self.assertTrue(got["reason"].startswith("ssh: Could not resolve hostname git.example.invalid"), got)
        self.assertLessEqual(len(got["reason"]), 200)

    def test_the_choice_of_line(self) -> None:
        import scan_remotes
        self.assertEqual(scan_remotes.failure_reason(
            "warning: redirecting\nfatal: could not read Username for 'https://example.invalid': "
            "terminal prompts disabled\n"),
            "fatal: could not read Username for 'https://example.invalid': terminal prompts disabled")
        self.assertEqual(scan_remotes.failure_reason("\n\n"), "unknown")
        self.assertEqual(scan_remotes.failure_reason("something odd\nmore"), "something odd")
        self.assertEqual(len(scan_remotes.failure_reason("fatal: " + "x" * 400)), 200)


class NoBareGit(unittest.TestCase):
    """Structural: no engine module spawns git except through `safe_git`.

    A new call site written the old way would pass every behavioural case above
    that does not happen to drive it; this reads every module instead.
    """

    #: Files allowed to spell a git argv themselves, each with why.
    ALLOWED = {
        "safe_git.py": "the door itself",
        "fabric_service.py": "vendored byte-for-byte (its digest is checked); its build_info is not called by the engine",
    }
    SPAWNERS = {"run", "Popen", "check_output", "check_call", "call"}

    @staticmethod
    def _is_git(node: ast.AST) -> bool:
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return os.path.basename(first.value) == "git"
            return isinstance(first, ast.Name) and first.id in {"GIT", "GIT_BIN"}
        return False

    def test_every_git_spawn_goes_through_the_door(self) -> None:
        offenders = []
        for path in sorted(ROOT.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("tests/") or rel in self.ALLOWED or "__pycache__" in rel:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), rel)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in self.SPAWNERS and node.args and self._is_git(node.args[0])):
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(offenders, [], "git spawned outside safe_git")

    def test_the_guard_sees_a_bare_call(self) -> None:
        tree = ast.parse('subprocess.run(["git", "-C", repo, "status"])')
        call = tree.body[0].value
        self.assertTrue(self._is_git(call.args[0]))


class MissingRegistrationReceipt(unittest.TestCase):
    """An auto `worktree-missing` action drops ONE registration, only after the directory is proven
    absent and its HEAD is kept by a ref. A repository-wide `git worktree prune` once dropped the
    record of a detached worktree on a renamed volume, and its commit became unreachable."""

    def setUp(self) -> None:
        import cleanup
        self.cleanup = cleanup
        self.d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-missing-wt-")).resolve()
        self.addCleanup(self._restore_modes)
        self.repo = self.d / "alpha-web"
        bare(self.d, "init", "-q", "-b", "main", str(self.repo))
        bare(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        self.vol = self.d / "volume"
        self.vol.mkdir()

    def _restore_modes(self) -> None:
        for p in (self.d / "volume", self.d / "volume-renamed"):
            if p.exists():
                p.chmod(0o755)

    def detached(self, path: pathlib.Path) -> str:
        bare(self.repo, "worktree", "add", "-q", "--detach", str(path))
        bare(path, "commit", "-q", "--allow-empty", "-m", "the only commit of this worktree")
        return bare(path, "rev-parse", "HEAD").stdout.strip()

    def registered(self) -> list[str]:
        out = bare(self.repo, "worktree", "list", "--porcelain").stdout
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    def act(self, target: pathlib.Path, head: str | None) -> dict:
        return self.cleanup.act({"class": "worktree-missing", "tier": "auto", "repo": str(self.repo),
                                 "target": str(target), "head": head}, self.d / "archive")

    def reachable(self, sha: str) -> bool:
        refs = bare(self.repo, "for-each-ref", "--contains", sha, "--format=%(refname)").stdout.split()
        return bool(refs)

    def test_a_renamed_volume_is_unknown_and_its_registration_stays(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        self.vol.rename(self.d / "volume-renamed")
        row = self.act(wt, head)
        self.assertEqual(row["result"], "skipped", row)
        self.assertEqual(row["reason_code"], "presence-unknown")
        self.assertIn(str(wt), self.registered())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "a permission-denied parent needs POSIX modes and a non-root user")
    def test_a_permission_denied_parent_is_unknown(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        self.vol.chmod(0)
        row = self.act(wt, head)
        self.vol.chmod(0o755)
        self.assertEqual(row["result"], "skipped", row)
        self.assertEqual(row["reason_code"], "presence-unknown")
        self.assertIn(str(wt), self.registered())

    def test_an_absent_detached_worktree_keeps_its_commit_under_an_archive_ref(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        other = self.d / "elsewhere" / "wt2"
        other.parent.mkdir()
        other_head = self.detached(other)
        shutil.rmtree(wt)
        other.parent.rename(self.d / "elsewhere-renamed")     # missing to Git, unknown to us
        row = self.act(wt, head)
        self.assertEqual(row["result"], "pruned", row)
        self.assertEqual(row["scope"], "one-registration")
        self.assertTrue(row["retired_entry"].startswith("worktrees-retired/"))
        self.assertEqual(row["retained_ref"], f"refs/heads/archive/wt-{head[:8]}")
        self.assertEqual(bare(self.repo, "rev-parse", row["retained_ref"]).stdout.strip(), head)
        self.assertNotIn(str(wt), self.registered())
        self.assertIn(str(other), self.registered(), "a repository-wide prune would drop this one too")
        bare(self.repo, "gc", "-q", "--prune=now")
        self.assertTrue(self.reachable(head))
        self.assertEqual(bare(self.repo, "cat-file", "-t", other_head).stdout.strip(), "commit")

    def test_an_absent_branch_worktree_is_kept_by_its_branch(self) -> None:
        wt = self.vol / "topic"
        bare(self.repo, "worktree", "add", "-q", "-b", "topic", str(wt))
        bare(wt, "commit", "-q", "--allow-empty", "-m", "topic work")
        head = bare(wt, "rev-parse", "HEAD").stdout.strip()
        shutil.rmtree(wt)
        row = self.act(wt, head)
        self.assertEqual(row["result"], "pruned", row)
        self.assertEqual(row["retained_ref"], "refs/heads/topic")
        self.assertTrue(self.reachable(head))

    def test_unknown_or_changed_head_prunes_nothing(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        shutil.rmtree(wt)
        for given in (None, "0" * 40):
            with self.subTest(head=given):
                row = self.act(wt, given)
                self.assertEqual(row["result"], "skipped", row)
                self.assertIn("nothing pruned", row["reason"])
                self.assertIn(str(wt), self.registered())
        self.assertTrue(head)

    def test_a_reappeared_directory_prunes_nothing(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        row = self.act(wt, head)
        self.assertEqual((row["result"], row["reason"]), ("skipped", "the directory exists again"))

    def test_a_failed_move_never_reports_pruned_or_leaks_its_error(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        shutil.rmtree(wt)
        with patch.object(self.cleanup.os, "rename", side_effect=OSError("SECRET-ERROR")):
            row = self.act(wt, head)
        self.assertEqual((row["result"], row["effect"]), ("failed", "unknown"))
        self.assertEqual(row["reason_code"], "registration-prune-incomplete")
        self.assertIsNone(row["registration_changes"])
        self.assertNotIn("SECRET", str(row))
        self.assertIn(str(wt), self.registered())

    def test_a_timeout_has_uncertain_effect_and_no_raw_exception(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        shutil.rmtree(wt)
        real = self.cleanup.git

        def hanging(repo, *args, **kw):
            if args[:2] == ("rev-parse", "--git-common-dir"):
                raise subprocess.TimeoutExpired("SECRET-COMMAND", 1, output="SECRET-OUTPUT")
            return real(repo, *args, **kw)
        with patch.object(self.cleanup, "git", side_effect=hanging):
            row = self.act(wt, head)
        self.assertEqual((row["result"], row["effect"]), ("failed", "unknown"))
        self.assertNotIn("SECRET", str(row))

    def test_a_directory_that_reappears_at_the_last_moment_loses_nothing(self) -> None:
        # Between the last presence check and the move, the directory comes back clean with a
        # new detached commit. Only the administrative entry moves; the directory and its
        # commit stay, and moving the entry back plus `git worktree repair` registers it again.
        wt = self.vol / "wt"
        head = self.detached(wt)
        held = self.d / "held"
        os.rename(wt, held)
        real_rename = os.rename
        late = {}

        def racing(src, dst, *rest, **kw):
            if "worktrees-retired" in str(dst):
                real_rename(held, wt)
                bare(wt, "commit", "-q", "--allow-empty", "-m", "late")
                late["sha"] = bare(wt, "rev-parse", "HEAD").stdout.strip()
            return real_rename(src, dst, *rest, **kw)
        with patch.object(self.cleanup.os, "rename", side_effect=racing):
            row = self.act(wt, head)
        self.assertEqual(row["result"], "pruned", row)
        self.assertTrue((wt / ".git").exists(), "the working directory was not touched")
        self.assertEqual(bare(self.repo, "cat-file", "-t", late["sha"]).stdout.strip(), "commit")
        common = self.repo / ".git"
        retired = common / row["retired_entry"]
        self.assertEqual((retired / "HEAD").read_text().strip(), late["sha"])
        real_rename(retired, common / "worktrees" / "wt")
        bare(self.repo, "worktree", "repair", str(wt))
        self.assertIn(str(wt), self.registered())

    def test_a_symbolic_archive_ref_is_not_retention(self) -> None:
        wt = self.vol / "wt"
        head = self.detached(wt)
        shutil.rmtree(wt)
        bare(self.repo, "symbolic-ref", f"refs/heads/archive/wt-{head[:8]}", "refs/heads/main")
        row = self.act(wt, head)
        self.assertEqual(row["result"], "skipped", row)
        self.assertIn("symbolic", row["reason"])
        self.assertIn(str(wt), self.registered())

    def test_the_survey_marks_an_unknown_directory_unreadable_not_missing(self) -> None:
        import scan_git_hygiene
        gone, unknown = self.d / "gone-wt", self.vol / "wt"
        self.detached(gone)
        self.detached(unknown)
        shutil.rmtree(gone)
        self.vol.rename(self.d / "volume-renamed")
        rows = {w["path"]: w for w in scan_git_hygiene.worktrees(str(self.repo), set(), time.time())}
        self.assertEqual(rows[str(gone)]["state"], "missing")
        self.assertEqual(rows[str(unknown)]["state"], "unreadable")
        self.assertEqual(rows[str(unknown)]["presence"], "unknown")
        import cleanup
        planned = cleanup.plan_git({"checkouts": [{"path": str(self.repo), "repository": "repository:example-org/alpha-web",
                                                   "default_branch": "main", "branches": [],
                                                   "worktrees": list(rows.values())}]}, cleanup.load_config())
        self.assertEqual([(a["class"], a["target"]) for a in planned], [("worktree-missing", str(gone))])
        self.assertEqual(planned[0]["head"], rows[str(gone)]["head"])


class LocalUpstreamIsNotACopy(unittest.TestCase):
    """`branch.X.remote=.` makes a local branch the upstream; "identical to its upstream" then
    proves no copy exists elsewhere, least of all when the upstream is the branch itself."""

    def setUp(self) -> None:
        import cleanup
        self.cleanup = cleanup
        self.d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-local-upstream-")).resolve()
        self.origin, self.repo = self.d / "origin.git", self.d / "alpha-web"
        bare(self.d, "init", "-q", "--bare", "-b", "main", str(self.origin))
        bare(self.d, "init", "-q", "-b", "main", str(self.repo))
        bare(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        bare(self.repo, "remote", "add", "origin", str(self.origin))
        bare(self.repo, "branch", "topic")
        bare(self.repo, "checkout", "-q", "topic")
        bare(self.repo, "commit", "-q", "--allow-empty", "-m", "only here")
        bare(self.repo, "checkout", "-q", "main")

    def why(self) -> str:
        return self.cleanup.still_safe_branch({"class": "branch-pushed", "repo": str(self.repo),
                                               "target": "topic", "default_branch": "main"})

    def test_the_branch_as_its_own_upstream_refuses(self) -> None:
        bare(self.repo, "config", "branch.topic.remote", ".")
        bare(self.repo, "config", "branch.topic.merge", "refs/heads/topic")
        self.assertEqual(self.why(), "branch upstream is local, not a remote copy")

    def test_another_local_branch_as_upstream_refuses(self) -> None:
        bare(self.repo, "branch", "mirror", "topic")
        bare(self.repo, "config", "branch.topic.remote", ".")
        bare(self.repo, "config", "branch.topic.merge", "refs/heads/mirror")
        self.assertEqual(self.why(), "branch upstream is local, not a remote copy")

    def test_a_pushed_remote_tracking_upstream_still_passes(self) -> None:
        bare(self.repo, "push", "-q", "-u", "origin", "topic")
        self.assertEqual(self.why(), "")


class UnknownHoldersAreNamed(unittest.TestCase):
    def test_unknown_holder_coverage_is_not_reported_as_a_working_process(self) -> None:
        import cleanup
        import holders
        d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-holders-")).resolve()
        with patch.object(cleanup, "_LISTING", (time.monotonic(), holders.Holders(None))), \
                patch.object(cleanup, "busy_now", return_value=True):
            row = cleanup.act({"class": "worktree-clean", "tier": "auto", "repo": str(d),
                               "target": str(d / "wt")}, d / "archive")
        self.assertEqual(row["reason"], "holders could not be checked; nothing removed")
        with patch.object(cleanup, "_LISTING", (time.monotonic(), holders.Holders(["/elsewhere"]))), \
                patch.object(cleanup, "busy_now", return_value=True):
            row = cleanup.act({"class": "worktree-clean", "tier": "auto", "repo": str(d),
                               "target": str(d / "wt")}, d / "archive")
        self.assertEqual(row["reason"], "a process works inside it now")


if __name__ == "__main__":
    unittest.main()
