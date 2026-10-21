"""No git the project starts in a repository of its own may leave background maintenance running.

Git starts `gc --auto` (and `maintenance run --auto`) by itself after `commit`, `fetch`, `merge`,
`am` and `rebase`, detached, and it outlives the command. In a temporary repository it races the
cleanup: on 2026-10-20 check.yml failed on e6e8673 with "OSError: directory not empty" while a
test removed its `.git`, and the release guard refused v0.27.0. #321 fixed the restore; these
cases close the class, in three layers:

- **At run time.** The portable runner's `clean_env` (engine suites) and check.yml's
  workflow-level `env:` (every CI step, the root tests included) carry `gc.auto=0` and
  `maintenance.auto=false` in `GIT_CONFIG_COUNT`. `RootTestsRunQuiet` pins the workflow block to
  the helper's own output, and, on GitHub Actions, proves git reads both settings in this very
  process.
- **In the tests' source.** Every test's git environment comes from
  `observatory/engine/tests/git_env.py`. `TestsUseTheHelper` refuses a test that sets or removes a
  `GIT_CONFIG_*` variable itself, strips every `GIT_*` variable by hand (which also drops the
  runner's settings), or hands git an environment built as a literal dict.
- **In the code.** Every git command the engine, the launcher and `tools/` spell that is not a
  pure read carries `-c gc.auto=0 -c maintenance.auto=false` among git's own options, as
  `safe_git.OPTIONS` does for the rest. Passing `-c` changes nothing in the person's
  configuration; it only stops this command from starting work that outlives it.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "observatory/engine/tests/git_env.py"
WORKFLOW = ROOT / ".github/workflows/check.yml"

_spec = importlib.util.spec_from_file_location("observatory_test_git_env", HELPER)
git_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(git_env)

QUIET = ("gc.auto=0", "maintenance.auto=false")

#: Test files that may name a `GIT_CONFIG_*` variable in an environment they build, with why.
HAND_BUILT_ALLOWED = {
    "observatory/engine/tests/git_env.py": "the helper itself",
    "observatory/engine/tests/test_git_env.py": "the helper's own cases feed it environments to compose",
}

#: Subcommands that start no background maintenance. Anything else, or a subcommand the scan
#: cannot read (spliced in from a variable), must carry both settings.
READ_ONLY = frozenset({
    "--version", "version", "config", "var", "rev-parse", "status", "log", "show", "diff", "ls-files",
    "ls-tree", "ls-remote", "for-each-ref", "cat-file", "rev-list", "symbolic-ref", "show-ref",
    "merge-base", "describe", "check-ignore", "remote", "branch", "init",
})

#: Calls whose positional arguments ARE the argv (`create_subprocess_exec("git", "fetch")`).
EXEC_STYLE = frozenset({"create_subprocess_exec", "spawnlp", "spawnl", "execlp", "execl"})

#: Calls that take a whole command line as one string. A string handed to anything else (a
#: message, an exception) is not read as a command.
SHELL_STYLE = frozenset({"run", "Popen", "call", "check_call", "check_output", "system", "popen",
                         "getoutput", "getstatusoutput", "create_subprocess_shell"})

#: Calls that take an argv but start nothing: a result or an error being built.
NOT_A_SPAWN = frozenset({"CompletedProcess", "CalledProcessError", "TimeoutExpired"})


# --- names bound in a scope -----------------------------------------------------------------

Scopes = list  # innermost first: each a dict name -> [assigned value, ...]


def bindings(scope: ast.AST) -> dict[str, list[ast.expr]]:
    """Every `name = value` and `name += value` in one scope, not inside nested functions."""
    out: dict[str, list[ast.expr]] = {}
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out.setdefault(node.targets[0].id, []).append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            out.setdefault(node.target.id, []).append(node.value)
        stack.extend(ast.iter_child_nodes(node))
    return out


def bound(name: str, scopes: Scopes) -> list[ast.expr] | None:
    return next((s[name] for s in scopes if name in s), None)


def walk_scopes(tree: ast.AST):
    """`(node, scopes)` for every node, scopes innermost first."""
    def visit(scope, scopes):
        for node in ast.iter_child_nodes(scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                inner = [bindings(node), *scopes]
                yield node, inner
                yield from visit(node, inner)
                continue
            yield node, scopes
            yield from visit(node, scopes)
    yield from visit(tree, [bindings(tree)])


# --- reading an argv ------------------------------------------------------------------------

def _words(text: str) -> list[ast.expr]:
    try:
        return [ast.Constant(w) for w in shlex.split(text)]
    except ValueError:
        return [ast.Constant(w) for w in text.split()]


def as_argv(expr: ast.expr | None, scopes: Scopes, depth: int = 0, text: bool = False) -> list[ast.expr] | None:
    """The elements of an argv expression, splices and names resolved where the scope says what
    they hold; None when it is not a sequence the scan can read. A string is read as a command
    line only with `text` (a shell-style call)."""
    if expr is None or depth > 6:
        return None
    if isinstance(expr, (ast.List, ast.Tuple)):
        out: list[ast.expr] = []
        for e in expr.elts:
            inner = e.value if isinstance(e, ast.Starred) else None
            spliced = as_argv(inner, scopes, depth + 1) if isinstance(inner, ast.Name) else None
            out += spliced if spliced is not None else [e]
        return out
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left, right = as_argv(expr.left, scopes, depth + 1, text), as_argv(expr.right, scopes, depth + 1, text)
        if left is not None:
            return left + (right if right is not None else [ast.Starred(expr.right)])
        return None
    if isinstance(expr, ast.Name):
        values = bound(expr.id, scopes)
        # A tuple bound to a name is a key or a record (`key = (git, home)`), not an argv.
        if not values or any(isinstance(v, ast.Tuple) for v in values):
            return None
        out = []
        for v in values:
            part = as_argv(v, scopes, depth + 1, text)
            if part is None:
                return None
            out += part
        return out
    if not text:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return _words(expr.value)
    if isinstance(expr, ast.JoinedStr):
        out = []
        for v in expr.values:
            out += _words(v.value) if isinstance(v, ast.Constant) else [ast.Starred(v)]
        return out
    return None


def is_git(e: ast.expr, scopes: Scopes, depth: int = 0) -> bool:
    """A literal git, a name that says git, or a name bound to `which("git")`/`safe_git.binary()`."""
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return os.path.basename(e.value) == "git"
    if isinstance(e, ast.Name):
        if e.id.lower() in {"git", "git_bin"}:
            return True
        return depth < 4 and any(is_git(v, scopes, depth + 1) for v in bound(e.id, scopes) or [])
    if isinstance(e, ast.Call):
        name = e.func.attr if isinstance(e.func, ast.Attribute) else getattr(e.func, "id", "")
        if name == "binary":
            return True
        if name == "which":
            return bool(e.args) and is_git(e.args[0], scopes, depth + 1)
    if isinstance(e, ast.BoolOp):
        return any(is_git(v, scopes, depth + 1) for v in e.values)
    return False


def spawned_argv(node: ast.Call, scopes: Scopes) -> list[ast.expr] | None:
    """The argv a call starts, read from its first argument, its `args=` keyword, or (for the
    exec-style calls) its positional arguments; None when it starts no git the scan can see."""
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    if name in NOT_A_SPAWN:
        return None
    if name in EXEC_STYLE and node.args:
        argv = []
        for a in node.args:
            argv += (as_argv(a.value, scopes) or [a]) if isinstance(a, ast.Starred) else [a]
    else:
        first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
        # A command line as one string runs only through a shell-style call: read it there when
        # the call says shell=True, or when the string itself starts with git.
        shell = any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value for k in node.keywords)
        head = first
        while isinstance(head, ast.BinOp):
            head = head.left
        if isinstance(head, ast.JoinedStr):
            head = head.values[0] if head.values else None
        starts = isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value.startswith("git ")
        argv = as_argv(first, scopes, text=name in SHELL_STYLE and (shell or starts))
    if not argv or not is_git(argv[0], scopes):
        return None
    return argv


def global_options(argv: list[ast.expr]) -> tuple[set[str], str | None]:
    """The `-c` values git reads before the subcommand, and the subcommand (None when a variable
    stands where it is).

    A splice the scan cannot resolve (`*location`) is passed over when a spelled word follows:
    it holds options there. A word outside READ_ONLY is flagged either way, so passing over a
    splice can only hide one that holds the subcommand ahead of a read-only word."""
    settings: set[str] = set()
    i = 1
    while i < len(argv):
        e = argv[i]
        if isinstance(e, ast.Starred):
            i += 1
            continue
        if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
            return settings, None
        if e.value in {"-c", "-C"}:
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if e.value == "-c" and isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                settings.add(nxt.value)
            i += 2
            continue
        if e.value.startswith("--") and e.value != "--version":
            i += 1
            continue
        if e.value == "remote":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if not (isinstance(nxt, ast.Constant) and nxt.value not in {"update", "prune"}):
                return settings, "remote update"
        return settings, e.value
    return settings, None


def unquiet_git(source: str, filename: str = "<source>") -> list[str]:
    """Lines where `source` spawns git with an argv it spells, for a command that can start
    background maintenance, without both settings among git's own `-c` options."""
    offenders = []
    for node, scopes in walk_scopes(ast.parse(source, filename)):
        if isinstance(node, ast.Call):
            argv = spawned_argv(node, scopes)
            if argv is None:
                continue
            settings, sub = global_options(argv)
            if not all(q in settings for q in QUIET) and sub not in READ_ONLY:
                offenders.append(f"{node.lineno}")
    return offenders


# --- the test-side lint ---------------------------------------------------------------------

def _git_config_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("GIT_CONFIG"):
        return node.value
    return None


def _strips_git(node: ast.AST) -> bool:
    """A comprehension that filters on `startswith("GIT_")`: every GIT_ variable dropped by hand,
    the runner's settings with them."""
    if not isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return False
    for gen in node.generators:
        for cond in gen.ifs:
            for c in ast.walk(cond):
                if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "startswith"):
                    continue
                prefixes = [x for a in c.args for x in (a.elts if isinstance(a, ast.Tuple) else [a])]
                # "GIT_" or "GIT" (every git variable), not "GITHUB_".
                if any(isinstance(x, ast.Constant) and x.value in {"GIT", "GIT_", "GIT_CONFIG", "GIT_CONFIG_"}
                       for x in prefixes):
                    return True
    return False


def _from_helper(expr: ast.expr | None, scopes: Scopes, depth: int = 0) -> bool:
    """The environment expression comes from git_env(...): a call to it, a name bound to one, or a
    dict literal spreading one."""
    if expr is None or depth > 6:
        return False
    if isinstance(expr, ast.Call):
        name = expr.func.attr if isinstance(expr.func, ast.Attribute) else getattr(expr.func, "id", "")
        return name in {"git_env", "quiet_maintenance"}
    if isinstance(expr, ast.Name):
        values = bound(expr.id, scopes)
        return bool(values) and all(_from_helper(v, scopes, depth + 1) for v in values)
    if isinstance(expr, ast.Dict):
        return any(k is None and _from_helper(v, scopes, depth + 1) for k, v in zip(expr.keys, expr.values))
    return False


def _hand_built_env(expr: ast.expr | None, scopes: Scopes, depth: int = 0) -> bool:
    """An `env=` git gets that the source built itself: a dict literal, `dict(...)` or a
    comprehension, directly or through a name, and not from the helper."""
    if expr is None or depth > 6 or _from_helper(expr, scopes):
        return False
    if isinstance(expr, (ast.Dict, ast.DictComp)):
        return True
    if isinstance(expr, ast.Call) and getattr(expr.func, "id", "") == "dict":
        return True
    if isinstance(expr, ast.Name):
        return any(_hand_built_env(v, scopes, depth + 1) for v in bound(expr.id, scopes) or [])
    return False


def hand_built(source: str, filename: str = "<source>") -> list[str]:
    """Lines where a test builds a git environment by hand instead of through git_env:

    - it sets or removes a `GIT_CONFIG_*` variable itself (a dict key, a keyword, an item
      assignment or deletion, `setdefault`, `pop`, `putenv`);
    - it strips every `GIT_*` variable with a comprehension (`git_env(drop_git=True)` does it and
      keeps the settings);
    - it hands a git it starts an `env=` built as a literal dict, `dict(...)` or comprehension.
    """
    found = []
    for node, scopes in walk_scopes(ast.parse(source, filename)):
        names: list[str | None] = []
        if isinstance(node, ast.Dict):
            names = [_git_config_name(k) for k in node.keys if k is not None]
        elif isinstance(node, ast.Call):
            names = [k.arg if k.arg and k.arg.startswith("GIT_CONFIG") else None for k in node.keywords]
            if (isinstance(node.func, ast.Attribute) and node.func.attr in {"setdefault", "putenv", "pop", "unsetenv"}
                    and node.args):
                names.append(_git_config_name(node.args[0]))
            if spawned_argv(node, scopes) is not None:
                env = next((k.value for k in node.keywords if k.arg == "env"), None)
                if _hand_built_env(env, scopes):
                    names.append("env built by hand for git")
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names = [_git_config_name(node.slice)]
        if _strips_git(node):
            names.append("GIT_* stripped by hand")
        found += [f"{node.lineno}:{name}" for name in names if name]
    return found


def test_files() -> list[Path]:
    files = []
    for folder in (ROOT / "observatory/engine/tests", ROOT / "tests"):
        files += [p for p in sorted(folder.rglob("*.py")) if "__pycache__" not in p.parts]
    return files


class TestsUseTheHelper(unittest.TestCase):
    def test_no_test_builds_a_git_environment_by_hand(self):
        offenders = []
        for path in test_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel in HAND_BUILT_ALLOWED:
                continue
            offenders += [f"{rel}:{hit}" for hit in hand_built(path.read_text(encoding="utf-8"), rel)]
        self.assertEqual(offenders, [], "build it with git_env.git_env(...) (observatory/engine/tests/git_env.py)")

    def test_the_lint_sees_each_way_of_building_one(self):
        for snippet in ('env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}',
                        'env.update(GIT_CONFIG_NOSYSTEM="1")',
                        'os.environ["GIT_CONFIG_GLOBAL"] = path',
                        'env.setdefault("GIT_CONFIG_COUNT", "1")',
                        'env.pop("GIT_CONFIG_COUNT", None)',
                        'del os.environ["GIT_CONFIG_KEY_0"]',
                        'patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1"}, clear=True)',
                        # the four sites of the #323 review, as they were
                        'base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}',
                        'env = {k: v for k, v in os.environ.items() if not k.startswith(("OBSERVATORY_", "GIT_"))}',
                        'def git_repo(path):\n    env = {"GIT_AUTHOR_NAME": "t", "HOME": str(path)}\n'
                        '    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, env=env)',
                        'def git(self, *args):\n    env = {**os.environ, "GIT_AUTHOR_NAME": "F"}\n'
                        '    return subprocess.run(["git", "-C", root, *args], env=env)',
                        'subprocess.run(["git", "commit"], env=dict(os.environ, A="1"))'):
            self.assertTrue(hand_built(snippet), snippet)
        for snippet in ('value = env["GIT_CONFIG_GLOBAL"]', 'self.assertIn("GIT_CONFIG_GLOBAL", env)',
                        'env = git_env.git_env(HOME=home)',
                        'def f():\n    base = git_env(drop_git=True)\n    env = {**base, "GIT_AUTHOR_DATE": when}\n'
                        '    subprocess.run(["git", "commit"], env=env)',
                        'subprocess.run([sys.executable, "x.py"], env={"A": "1"})',
                        'subprocess.run(["git", "commit"])',
                        'env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}'):
            self.assertFalse(hand_built(snippet), snippet)

    def test_every_allowed_file_exists(self):
        for rel in HAND_BUILT_ALLOWED:
            self.assertTrue((ROOT / rel).is_file(), rel)


def workflow_env(text: str) -> dict[str, str]:
    """check.yml's workflow-level `env:` block (top level, applies to every job and step), read
    as text: the root tests carry no YAML parser."""
    m = re.search(r"^env:\n((?:  .*\n)+)", text, re.M)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, _, v = line.strip().partition(":")
        out[k.strip()] = v.strip().strip("'\"")
    return out


class RootTestsRunQuiet(unittest.TestCase):
    """The root tests run as `python -m unittest discover -s tests`, outside the portable runner,
    and `discover -s` imports no package `__init__` first: the settings reach them through the
    workflow's own environment."""

    def test_the_workflow_environment_is_the_helpers(self):
        self.assertEqual(workflow_env(WORKFLOW.read_text(encoding="utf-8")), git_env.quiet_maintenance({}))

    def test_the_reader_sees_only_the_top_level_block(self):
        text = "name: x\non: [push]\njobs:\n  a:\n    env:\n      GIT_CONFIG_COUNT: '2'\n"
        self.assertEqual(workflow_env(text), {})

    @unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true", "proves the CI run itself; not on GitHub Actions")
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_on_ci_git_reads_both_settings_in_this_process(self):
        with tempfile.TemporaryDirectory() as d:
            for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
                got = subprocess.run(["git", "config", "--get", key], cwd=d, capture_output=True, text=True)
                self.assertEqual(got.stdout.strip(), value, key)


# --- the engine-side guard ------------------------------------------------------------------

def code_files() -> list[Path]:
    """The engine, the launcher and the release tools; their tests are held by the lint above."""
    files = []
    for folder in (ROOT / "observatory", ROOT / "tools"):
        files += [p for p in sorted(folder.rglob("*.py"))
                  if "__pycache__" not in p.parts and "tests" not in p.relative_to(folder).parts]
    return files


class EngineGitIsQuiet(unittest.TestCase):
    def test_every_git_the_code_spells_turns_maintenance_off(self):
        offenders = []
        for path in code_files():
            rel = path.relative_to(ROOT).as_posix()
            offenders += [f"{rel}:{line}" for line in unquiet_git(path.read_text(encoding="utf-8"), rel)]
        self.assertEqual(offenders, [], "add -c gc.auto=0 -c maintenance.auto=false, or go through safe_git")

    def test_the_guard_sees_what_it_must(self):
        caught = (
            'subprocess.run(["git", "-C", repo, "commit", "-m", "x"])',
            'subprocess.run(["git", "-C", repo, *args])',
            'def f(git):\n    _run([git, "fetch", url])',
            'def f():\n    base = ["git", "-C", root]\n    subprocess.run([*base, *options, *args])',
            'def f():\n    argv = ["git", "pull"]\n    subprocess.run(argv)',
            'subprocess.run(["git", "remote", "update"])',
            # the review's LOW list
            'subprocess.run(["git", "-C", repo] + ["fetch"])',
            'await asyncio.create_subprocess_exec("git", "-C", repo, "fetch")',
            'subprocess.run(args=["git", "commit"])',
            'subprocess.run("git -C repo commit -m x", shell=True)',
            'subprocess.run(f"git -C {repo} fetch", shell=True)',
            'def f():\n    exe = shutil.which("git")\n    subprocess.run([exe, "fetch"])',
            'def f():\n    exe = safe_git.binary()\n    subprocess.run([exe, "commit"])',
            # the settings anywhere but among git's own options do not count
            'subprocess.run(["git", "commit", "-m", "gc.auto=0 maintenance.auto=false"])',
            'subprocess.run(["git", "commit", "-c", "gc.auto=0", "-c", "maintenance.auto=false"])',
        )
        for snippet in caught:
            self.assertTrue(unquiet_git(snippet), snippet)
        passed = (
            'subprocess.run(["git", "--version"])',
            'subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"])',
            'subprocess.run(["git", "remote", "get-url", "origin"])',
            'def f(git):\n    gitc = [git, "-c", "gc.auto=0", "-c", "maintenance.auto=false"]\n'
            '    _run([*gitc, "-C", dest, "fetch", bundle])',
            'subprocess.run(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "commit"])',
            'subprocess.run("git -c gc.auto=0 -c maintenance.auto=false commit", shell=True)',
            'subprocess.run([sys.executable, "-c", "pass"])',
            'subprocess.run("echo git commit", shell=True)',
            'raise Refused(f"git configuration could not be read: {why}")',
            'print(f"git hygiene: {n} checkouts")',
            'print(f"git hygiene: {n}" + (f"; {m} degraded" if m else ""))',
            'def f(git):\n    key = (git, home)\n    cache.get(key)',
            'subprocess.CompletedProcess([safe_git.binary(git), *args], 1, "", "")',
        )
        for snippet in passed:
            self.assertFalse(unquiet_git(snippet), snippet)


class TheDemoRepository(unittest.TestCase):
    """`observatory demo` commits into a sample repository it created: a repository of ours."""

    def test_every_git_the_demo_runs_turns_maintenance_off(self):
        from observatory import cli
        calls = []
        real = subprocess.run

        def recording(argv, *a, **kw):
            calls.append([str(x) for x in argv])
            return real(argv, *a, **kw)
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, git_env.git_env({"HOME": d, "PATH": os.environ.get("PATH", "")}, drop_git=True),
                           clear=True), \
                patch.object(cli.subprocess, "run", recording):
            cli.demo(Path(d).resolve() / "state")
        gits = [c for c in calls if os.path.basename(c[0]) == "git"]
        self.assertTrue(gits, "the demo ran no git")
        for argv in gits:
            for setting in QUIET:
                self.assertIn(setting, argv, argv[:8])


if __name__ == "__main__":
    unittest.main()
