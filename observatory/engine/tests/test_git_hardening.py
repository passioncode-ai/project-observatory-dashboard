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

ROOT = pathlib.Path(__file__).resolve().parents[1]
for sub in ("", "collectors", "tools", "plugins", "tests"):
    sys.path.insert(0, str(ROOT / sub) if sub else str(ROOT))
import tmp as tmpdir  # noqa: E402

HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "post-checkout",
         "post-merge", "post-rewrite", "pre-push", "pre-auto-gc", "reference-transaction",
         "post-index-change", "fsmonitor-watchman")
FAKE_SIG = ("gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEFAKEFAKEFAKE=\n =FAKE\n"
            " -----END PGP SIGNATURE-----\n")


def bare(cwd, *args, env=None, check=True, text=True, input=None):
    """Fixture construction only: git with NO user configuration at all."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    base.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
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
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.d / "xdg"), GIT_CONFIG_NOSYSTEM="1",
                   PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        return env


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
        env = dict(os.environ)
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
        cleanup.busy_now = lambda path: False
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
        patch = (pathlib.Path(rec["archive"]) / "tracked.patch").read_text(encoding="utf-8")
        self.assertIn("+uncommitted edit", patch)
        self.assertNothingRan("cleanup")


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


if __name__ == "__main__":
    unittest.main()
