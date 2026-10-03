#!/usr/bin/env python3
"""Every `project-observatory full …` command the engine hands a person is one the
dispatcher accepts, and every subcommand answers `--help` without running.

WHY THIS EXISTS. Findings, hooks, pages and messages tell the reader what to run
next, and seven of those commands were refused outright: `full google --force`
(the `analytics.stale` remedy), `full plugins --only ID --force` (the
`plugin.broken` and `plugin.stale` remedies), `full merge emit`, `full emit
findings` and `full scan merge emit` each answered "step arguments are not
accepted here" with exit 2. A remedy the program refuses is worse than none: the
reader did the right thing and was told no. Nothing checked the handed-over text
against the dispatcher, because the two live in different files and only a person
following the advice ever joined them.

HOW. The commands are read out of the shipped sources: Python string constants
through the syntax tree, so implicitly concatenated literals and f-strings are seen
whole; every other shipped text file as text; and the dashboard's
`fullCommand(...)` calls, which assemble the command in the browser. Each is given
to `observatory.refusal`, which parses it with the very parsers the dispatcher
uses and runs nothing. A placeholder (`PATH`, `{p['id']}`) is replaced by a sample
from `SAMPLES`; an unknown placeholder fails, so a new remedy cannot slip past
untested by inventing a new spelling.

`--help` is the other half of the same promise: `full machine --help` ran the
machine survey and `full cleanup --help` wrote a cleanup plan, because both read
their arguments by hand; several parsers announced themselves as
`observatory.py`. Every subcommand the launcher knows is driven with `--help` in a
subprocess against an initialized synthetic workspace whose files are compared
before and after.
"""
from __future__ import annotations
import ast
import concurrent.futures
import contextlib
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import observatory as cli                                              # noqa: E402
from run_portable import runtime_environment                           # noqa: E402

COMMAND = "project-observatory full "
#: Characters that end a command inside prose: quoting, punctuation, a comment.
STOP_CHARS = set("`\"'(),;|#[]<\\\n…*")
#: Words that end a command inside prose ("run X, then Y", "X if the ceiling…").
#: A prose word NOT listed here is read as an argument and the command is refused,
#: which is the strict direction: the fix is to quote the command in backticks.
STOP_WORDS = {"then", "and", "or", "to", "if", "for", "with", "—", "–", "-", "->", "→",
              "once", "after", "before", "when", "first", "again", "now?"}
#: Every placeholder a handed-over command may carry, and the sample it is
#: parsed with. Unknown placeholders fail the test rather than pass untested.
SAMPLES = {
    "PATH": "/srv/example-ws/projects", "PID": "4242", "FILE": "/srv/example-ws/profile.json",
    "ID": "alpha-plugin", "AMOUNT": "5", "VENDOR/MODEL": "example/model-a", "X.Y.Z": "0.12.0",
    "PORT": "8765", "NAME": "github", "SNAPSHOT": "/srv/example-ws/snapshot", "OUTPUT": "/srv/example-ws/out",
    "ID,ID": "example/model-a,example/model-b", "CEILING": "daily_ceiling", "ACTION": "status",
    "{p['id']}": "alpha-plugin", "{release.version}": "0.12.0",
    "{doc['engine_version']}": "0.12.0", "{source}": "projects", "{section}": "integrations",
    "{name}": "github", "{k}": "daily_ceiling",
    'shellArg(RUNTIME.user_home || "$HOME")': "/srv/example-ws/home",
}
PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9_./,]*[A-Z]|[A-Z]{2,}")
SHIPPED_TEXT = {".js", ".mjs", ".sh", ".json", ".md", ".html", ".txt"}


def _bracketed(text: str, i: int, opener: str, closer: str) -> int:
    """Index just past the closer matching the opener at `i`."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == opener:
            depth += 1
        elif text[j] == closer:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def tokens(rest: str) -> list[str]:
    """The command's words, read the way a person copying it would stop."""
    out: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == " ":
            i += 1
            continue
        if rest.startswith("${", i) or rest[i] == "{":
            j = _bracketed(rest, i + (rest[i] == "$"), "{", "}")
            out.append(rest[i:j])
            i = j
            continue
        if rest[i] == '"':
            # A quoted value (`configure sources projects "$HOME/projects"`); a
            # quote that closes nowhere before a space is prose, and ends it.
            j = rest.find('"', i + 1)
            if j < 0 or " " in rest[i + 1:j]:
                break
            out.append(rest[i + 1:j])
            i = j + 1
            continue
        if rest[i] == "<":
            j = rest.find(">", i)
            if j < 0:
                break
            out.append(rest[i:j + 1])
            i = j + 1
            continue
        j = i
        while j < len(rest) and rest[j] != " " and rest[j] not in STOP_CHARS:
            j += 1
        word = rest[i:j]
        if not word or word in STOP_WORDS:
            break
        stripped = word.rstrip(".:!?")
        if stripped:
            out.append(stripped)
        if stripped != word or (j < len(rest) and rest[j] in STOP_CHARS):
            break
        i = j
    return out


def commands_in(text: str) -> list[list[str]]:
    found = []
    for m in re.finditer(re.escape(COMMAND), text):
        words = tokens(text[m.end():])
        if words:
            found.append(words)
    for m in re.finditer(r"\bfullCommand\(", text):
        end = _bracketed(text, m.end() - 1, "(", ")")
        inner, args, depth, start = text[m.end():end - 1], [], 0, 0
        for k, ch in enumerate(inner + ","):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(inner[start:k].strip())
                start = k + 1
        words: list[str] = []
        for arg in filter(None, args):
            literal = re.fullmatch(r'"([^"\\]*)"', arg)
            words += literal.group(1).split() if literal else [arg]
        if words:
            found.append(words)
    return found


def python_strings(path: Path):
    """Each string constant, f-strings whole with `{expr}` where a value goes.

    An f-string's literal pieces are skipped on their own (the whole is read), and
    so is a parser's `prog=`: that names the program, it hands nothing over."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    skip |= {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.keyword) and n.arg == "prog"}
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                else:
                    parts.append("{" + ast.unparse(value.value) + "}")
            yield node.lineno, "".join(parts)


def markdown_text(text: str) -> str:
    """Prose lines of a paragraph joined as a reader sees them, so a command
    wrapped across two source lines is read whole; fenced lines stay lines."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append("\n")
        elif fenced or not line.strip():
            out.append("\n" + line + "\n")
        else:
            out.append(line.strip() + " ")
    return "".join(out)


def shipped_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if not path.is_file() or rel.parts[0] in {"tests", ".venv", "node_modules"} \
                or "__pycache__" in rel.parts:
            continue
        if path.suffix == ".py" or path.suffix in SHIPPED_TEXT:
            yield rel, path


def handed_commands() -> dict[tuple[str, ...], list[str]]:
    """{argv (placeholders as written): [where, …]} over every shipped file."""
    seen: dict[tuple[str, ...], list[str]] = {}
    for rel, path in shipped_files():
        if path.suffix == ".py":
            texts = list(python_strings(path))
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            texts = [(1, markdown_text(text) if path.suffix == ".md" else text)]
        for line, text in texts:
            for argv in commands_in(text):
                seen.setdefault(tuple(argv), []).append(f"{rel}:{line}")
    return seen


def concrete(argv: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """(argv with samples substituted, placeholders without a sample)."""
    out, unknown = [], []
    for word in argv:
        if word in SAMPLES:
            out.append(SAMPLES[word])
        elif word.startswith(("{", "${", "<")) or (PLACEHOLDER.fullmatch(word) and not word.startswith("-")):
            unknown.append(word)
            out.append(word)
        else:
            out.append(word)
    return out, unknown


class HandedCommands(unittest.TestCase):
    def test_every_handed_over_full_command_is_accepted(self):
        found = handed_commands()
        refused = []
        for argv, where in sorted(found.items()):
            words, unknown = concrete(argv)
            if unknown:
                refused.append(f"{where[0]}: {' '.join(argv)} — no sample for {unknown}; add one to SAMPLES")
                continue
            reason = cli.refusal(words)
            if reason:
                refused.append(f"{where[0]}: project-observatory full {' '.join(argv)} — {reason}")
        self.assertEqual(refused, [], "handed-over commands the dispatcher refuses:\n  " + "\n  ".join(refused))
        # The scanner reaches the places that hand commands over; a regression in
        # the reader must not turn this into a vacuous pass.
        self.assertGreater(len(found), 60, f"only {len(found)} distinct commands were found")
        for expected in [("google", "--force"), ("plugins", "--only", "{p['id']}", "--force"),
                         ("local",), ("machine", "--explain", "PID"), ("configure", "sources", "projects", "PATH")]:
            self.assertIn(expected, found, f"the scanner no longer sees {expected}")

    def test_the_check_refuses_what_the_dispatcher_refuses(self):
        for argv in (["merge", "emit"], ["emit", "findings"], ["scan", "merge", "emit"],
                     ["google", "--forse"], ["plugins", "--only", "../outside"], ["plugins", "--only"],
                     ["local", "--force"], ["machine", "--frobnicate"], ["cleanup", "manual"],
                     ["configure", "colours", "x", "y"], ["no-such-step"], ["profile"],
                     ["update", "--apply", "--check"]):
            with self.subTest(argv):
                self.assertTrue(cli.refusal(argv), f"{argv} was accepted")
        for argv in (["google", "--force"], ["plugins", "--only", "alpha-plugin", "--force"],
                     ["plugins", "--only=alpha-plugin"], ["local"], ["check", "--suite", "machine"],
                     ["local", "--expect-skipped=smoke"], ["machine", "--explain", "4242"],
                     ["cleanup", "--apply", "--include", "manual"], ["google", "--help"]):
            with self.subTest(argv):
                self.assertEqual(cli.refusal(argv), "", f"{argv} was refused")

    def test_the_scanner_reads_prose_the_way_a_person_copies_it(self):
        text = ("run `project-observatory full scan-vault`, then project-observatory "
                "full local — or project-observatory full wallet if the ceiling stopped it; "
                "project-observatory full heroku, then x. <code>project-observatory full open</code>")
        self.assertEqual(commands_in(text), [["scan-vault"], ["local"], ["wallet"], ["heroku"], ["open"]])
        self.assertEqual(commands_in('fullCommand("configure sources projects", "PATH")'),
                         [["configure", "sources", "projects", "PATH"]])

    def test_forwarded_options_reach_the_tool_and_only_a_single_step(self):
        def invoke(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                return cli.main(["observatory.py", *args]), out.getvalue()
        with patch.object(cli.configuration, "validate_workspace"), \
             patch.object(cli.configuration, "load"), patch.object(cli.paths, "tighten"), \
             patch.object(cli, "_run_group", return_value=0) as group:
            self.assertEqual(invoke("google", "--force")[0], 0)
            self.assertEqual(group.call_args.kwargs["forwarded"], ["--force"])
            self.assertEqual(invoke("plugins", "--only=alpha-plugin", "--force")[0], 0)
            self.assertEqual(group.call_args.kwargs["forwarded"], ["--only", "alpha-plugin", "--force"])
            group.reset_mock()
            code, output = invoke("merge", "emit")
            self.assertEqual(code, 2)
            self.assertIn("One step per invocation", output)
            code, output = invoke("plugins", "--only", "../outside")
            self.assertEqual(code, 2)
            group.assert_not_called()

    def test_run_appends_forwarded_options_to_the_step_command(self):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.stdout = io.StringIO("")
        process.wait.return_value = 0
        with patch.object(cli.subprocess, "Popen", return_value=process) as spawn, \
             contextlib.redirect_stdout(io.StringIO()):
            cli.run("google", extra=["--force"])
        self.assertEqual(spawn.call_args.args[0], [*cli.STEPS["google"], "--force"])


def _tree(base: Path) -> dict[str, str]:
    out = {}
    for p in sorted(base.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(base))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class SubcommandHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="observatory-help-")
        base = Path(cls.temp.name).resolve()
        (base / "user").mkdir()
        cls.home = base / "workspace"
        cls.env = {**runtime_environment(), "PATH": os.environ.get("PATH", ""), "HOME": str(base / "user"),
                   "OBSERVATORY_HOME": str(cls.home), "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C",
                   "TMPDIR": str(base / "user")}
        init = cls.run_cli("init")
        assert init.returncode == 0, init.stdout + init.stderr

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def run_cli(cls, *args):
        return subprocess.run([sys.executable, str(ROOT / "observatory.py"), *args], cwd=ROOT,
                              env=cls.env, text=True, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)

    def subcommands(self) -> list[str]:
        public = cli.public_profile()
        names = set(cli.WORKSPACE_COMMANDS) | {"assistant", "check", "check-portable"}
        names |= {n for n in cli.STEPS if not cli.unavailable_step(n, public)}
        names |= {n for n in cli.GROUPS if not cli.unavailable_step(n, public)}
        return sorted(names)

    def test_every_subcommand_help_exits_zero_names_itself_and_runs_nothing(self):
        before = _tree(self.home)
        names = self.subcommands()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(names, pool.map(lambda n: self.run_cli(n, "--help"), names)))
        wrong = []
        for name, p in results.items():
            usage = f"usage: project-observatory full {name}"
            if p.returncode != 0 or usage not in p.stdout:
                wrong.append(f"{name}: exit {p.returncode}; {(p.stdout + p.stderr).strip()[:160]!r}")
        self.assertEqual(wrong, [], "subcommands whose --help is not help:\n  " + "\n  ".join(wrong))
        self.assertEqual(_tree(self.home), before, "a --help wrote into the workspace")
        self.assertGreater(len(names), 200)

    def test_full_help_lists_every_workspace_command(self):
        p = self.run_cli("--help")
        self.assertEqual(p.returncode, 0)
        words = set(re.findall(r"[a-z][a-z-]+", p.stdout))
        self.assertEqual(sorted(set(cli.WORKSPACE_COMMANDS) - words), [])
        self.assertIn("migrate-local", p.stdout)

    def test_help_names_documents_by_a_path_an_installed_user_has(self):
        # `docs/CLI-COMPATIBILITY.md` exists only beneath the engine directory,
        # and `docs/macos/SPEC.md` is not shipped at all: a bare relative path in
        # help resolves nowhere from the directory a person types in.
        top = self.run_cli("--help").stdout
        self.assertIn('"$(project-observatory full-path)/docs/CLI-COMPATIBILITY.md"', top)
        self.assertNotRegex(top, r"(?<![/\w])docs/CLI-COMPATIBILITY\.md")
        assistant = self.run_cli("assistant", "--help")
        self.assertEqual(assistant.returncode, 0)
        self.assertNotIn("docs/macos", assistant.stdout)
        configure = self.run_cli("configure")
        self.assertEqual(configure.returncode, 2)
        self.assertIn("usage: project-observatory full configure", configure.stderr)


if __name__ == "__main__":
    unittest.main()
