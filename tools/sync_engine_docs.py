#!/usr/bin/env python3
"""Write the engine's copies of the user documents from docs/, and of the dependency lock.

    python tools/sync_engine_docs.py           # rewrite observatory/engine/docs/{ONBOARDING,COMPATIBILITY,AGENT-ONBOARDING}.md
                                               # and observatory/engine/requirements-full.lock
    python tools/sync_engine_docs.py --check   # exit 1 when a copy differs

The rule lives in tests/test_engine_doc_copies.py (`derived`), so the tool and
the check cannot disagree: test paths lose `observatory/engine/`, and a relative
link to a file the engine does not ship points at the repository.

Arguments are parsed, not searched for: `--help` once fell through to write mode,
and any unknown argument is refused rather than read as "write".
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_engine_doc_copies import COPIES, ENGINE_DOCS, FILE_COPIES, derived  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sync_engine_docs.py", description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 when a copy differs")
    check = ap.parse_args(argv).check
    stale = []
    for name in COPIES:
        want, path = derived(name), ENGINE_DOCS / name
        if path.read_text(encoding="utf-8") != want:
            stale.append(name)
            if not check:
                path.write_text(want, encoding="utf-8")
    for name, copy in FILE_COPIES.items():
        want = (ROOT / name).read_bytes()
        if not copy.is_file() or copy.read_bytes() != want:
            stale.append(str(copy.relative_to(ROOT)))
            if not check:
                copy.write_bytes(want)
    print(("stale: " if check else "written: ") + (", ".join(stale) or "none"))
    return 1 if stale and check else 0


if __name__ == "__main__":
    raise SystemExit(main())
