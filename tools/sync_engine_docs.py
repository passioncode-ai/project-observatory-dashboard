#!/usr/bin/env python3
"""Write the engine's copies of the user documents from docs/.

    python tools/sync_engine_docs.py           # rewrite observatory/engine/docs/{ONBOARDING,COMPATIBILITY,AGENT-ONBOARDING}.md
    python tools/sync_engine_docs.py --check   # exit 1 when a copy differs

The rule lives in tests/test_engine_doc_copies.py (`derived`), so the tool and
the check cannot disagree: test paths lose `observatory/engine/`, and a relative
link to a file the engine does not ship points at the repository.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_engine_doc_copies import COPIES, ENGINE_DOCS, derived  # noqa: E402

stale = []
for name in COPIES:
    want, path = derived(name), ENGINE_DOCS / name
    if path.read_text(encoding="utf-8") != want:
        stale.append(name)
        if "--check" not in sys.argv:
            path.write_text(want, encoding="utf-8")
print(("stale: " if "--check" in sys.argv else "written: ") + (", ".join(stale) or "none"))
sys.exit(1 if stale and "--check" in sys.argv else 0)
