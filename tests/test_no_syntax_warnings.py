"""Every Python file compiles without a SyntaxWarning.

An invalid escape such as an unraw backslash-s in a docstring compiles today with a SyntaxWarning, printed to stderr by
every process that imports the module. A tool that promises one sentence on stderr then prints three (the wave R1
integration, #582: `fabric_service` and `review_queue` on every platform), and a later Python makes it an error.
"""
import pathlib
import unittest
import warnings

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP = {".git", "node_modules", ".venv", "__pycache__"}


class NoSyntaxWarnings(unittest.TestCase):
    def test_every_python_file_compiles_without_a_warning(self):
        found = []
        for path in sorted(ROOT.rglob("*.py")):
            if SKIP.intersection(path.relative_to(ROOT).parts):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                compile(source, str(path), "exec")
            found += [f"{path.relative_to(ROOT)}: {w.message}" for w in caught if issubclass(w.category, SyntaxWarning)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
