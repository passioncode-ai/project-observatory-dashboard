"""No git the project starts in a repository of its own may leave background maintenance running.

Git starts `gc --auto` (and `maintenance run --auto`) by itself after `commit`, `fetch`, `merge`,
`am` and `rebase`, detached, and it outlives the command. In a temporary repository it races the
cleanup: on 2026-10-20 check.yml failed on e6e8673 with "OSError: directory not empty" while a
test removed its `.git`, and the release guard refused v0.27.0. #321 fixed the restore; these
cases close the class.

- **Tests** take every git environment from `observatory/engine/tests/git_env.py`, which turns
  maintenance off through `GIT_CONFIG_COUNT`. A test that builds one by hand (sets any
  `GIT_CONFIG_*` variable itself) is refused here, so the next one cannot forget it.
- **The engine, the launcher and the release tools**: every git command they spell that is not a pure
  read carries `-c gc.auto=0 -c maintenance.auto=false`, as `safe_git.OPTIONS` does for the
  rest. Passing `-c` changes nothing in the person's configuration; it only stops this command
  from starting work that outlives it.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "observatory/engine/tests/git_env.py"

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


# --- the test-side lint ---------------------------------------------------------------------

def _git_config_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("GIT_CONFIG"):
        return node.value
    return None


def hand_built(source: str, filename: str = "<source>") -> list[str]:
    """Lines where `source` puts a `GIT_CONFIG_*` variable into an environment itself: a dict key,
    a keyword (`update(GIT_CONFIG_GLOBAL=...)`), an item assignment or `setdefault`."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        names: list[str | None] = []
        if isinstance(node, ast.Dict):
            names = [_git_config_name(k) for k in node.keys if k is not None]
        elif isinstance(node, ast.Call):
            names = [k.arg if k.arg and k.arg.startswith("GIT_CONFIG") else None for k in node.keywords]
            if (isinstance(node.func, ast.Attribute) and node.func.attr in {"setdefault", "putenv"}
                    and node.args):
                names.append(_git_config_name(node.args[0]))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            names = [_git_config_name(node.slice)]
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
                        'patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1"}, clear=True)'):
            self.assertTrue(hand_built(snippet), snippet)
        for snippet in ('value = env["GIT_CONFIG_GLOBAL"]', 'self.assertIn("GIT_CONFIG_GLOBAL", env)',
                        'env = git_env.git_env(HOME=home)'):
            self.assertFalse(hand_built(snippet), snippet)

    def test_every_allowed_file_exists(self):
        for rel in HAND_BUILT_ALLOWED:
            self.assertTrue((ROOT / rel).is_file(), rel)


# --- the engine-side guard ------------------------------------------------------------------

def _lists_by_name(scope: ast.AST) -> dict[str, list[ast.expr]]:
    """`name = [...]` and `name += [...]` in one scope (not nested functions), elements joined.
    Lists only: an argv is a list, and a tuple bound to a name is a key or a record."""
    out: dict[str, list[ast.expr]] = {}
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.List)):
            out.setdefault(node.targets[0].id, []).extend(node.value.elts)
        elif (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
              and isinstance(node.value, ast.List)):
            out.setdefault(node.target.id, []).extend(node.value.elts)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _expand(elts: list[ast.expr], scopes: list[dict[str, list[ast.expr]]], depth: int = 0) -> list[ast.expr]:
    out: list[ast.expr] = []
    for e in elts:
        inner = e.value if isinstance(e, ast.Starred) else None
        if isinstance(inner, ast.Name) and depth < 4:
            bound = next((s[inner.id] for s in scopes if inner.id in s), None)
            if bound is not None:
                out += _expand(bound, scopes, depth + 1)
                continue
        out.append(e)
    return out


def _is_git(e: ast.expr) -> bool:
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return os.path.basename(e.value) == "git"
    return isinstance(e, ast.Name) and e.id.lower() in {"git", "git_bin"}


def _subcommand(argv: list[ast.expr]) -> str | None:
    """The subcommand after git's global options, or None when a variable stands where it is.

    A splice the scan cannot resolve (`*location`, `*(["-C", repo] if repo else [])`) is passed
    over when a spelled word follows it: it holds options there. A word outside READ_ONLY is
    flagged either way, so passing over a splice can only hide a splice that holds the
    subcommand ahead of a read-only word, which no caller writes."""
    i = 1
    while i < len(argv):
        e = argv[i]
        if isinstance(e, ast.Starred):
            i += 1
            continue
        if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
            return None
        if e.value in {"-c", "-C"}:
            i += 2
            continue
        if e.value.startswith("--") and e.value != "--version":
            i += 1
            continue
        if e.value == "remote":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if not (isinstance(nxt, ast.Constant) and nxt.value not in {"update", "prune"}):
                return "remote update"
        return e.value
    return None


def unquiet_git(source: str, filename: str = "<source>") -> list[str]:
    """Lines where `source` spawns git with an argv it spells, for a command that can start
    background maintenance, without both settings."""
    tree = ast.parse(source, filename)
    module = _lists_by_name(tree)
    offenders = []

    def visit(scope: ast.AST, scopes: list[dict[str, list[ast.expr]]]) -> None:
        for node in ast.iter_child_nodes(scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(node, [_lists_by_name(node), *scopes])
                continue
            if isinstance(node, ast.Call) and node.args:
                first = node.args[0]
                elts = None
                if isinstance(first, (ast.List, ast.Tuple)):
                    elts = first.elts
                elif isinstance(first, ast.Name):
                    elts = next((s[first.id] for s in scopes if first.id in s), None)
                argv = _expand(list(elts), scopes) if elts else []
                if argv and _is_git(argv[0]):
                    values = {e.value for e in argv if isinstance(e, ast.Constant)}
                    if not all(q in values for q in QUIET) and _subcommand(argv) not in READ_ONLY:
                        offenders.append(f"{node.lineno}")
            visit(node, scopes)

    visit(tree, [module])
    return offenders


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
            'subprocess.run([sys.executable, "-c", "pass"])',
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
