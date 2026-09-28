#!/usr/bin/env python3
"""The project a suite measures as "the observatory's own", resolved — never hardcoded.

Several suites assert on one particular project in the registry. In the
original installation that was the observatory's own repository, and a literal
project id held at six call sites broke the day the merge re-anchored the
project from its repository to its vault folder: the canonical id changed and
`survey.project_detail` answered "unknown project" — an error dict the
assertions then indexed into.

The ambient state here is the registry's own naming of the project under test,
which the merge is ALLOWED to change — anchoring follows evidence, and evidence
moves. A constant cannot track it, so the suites resolve it, and
`assert_resolvable()` makes a failed resolution say so in one line instead of
surfacing as a KeyError six frames away.

In the portable engine the subject is the synthetic estate's first project
(see `test_portable_mcp.setup`). `OBSERVATORY_TEST_OWN_REPO` and
`OBSERVATORY_TEST_OWN_FOLDER` point the resolver at another project when a
suite builds a different estate.
"""
from __future__ import annotations
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paths                                                      # noqa: E402
#: The repository the suites treat as the observatory's own. Its `owner/name`
#: is the durable fact — the project id built around it is not.
OWN_NWO = os.environ.get("OBSERVATORY_TEST_OWN_REPO", "example/sample-0")
OWN_FOLDER = os.environ.get("OBSERVATORY_TEST_OWN_FOLDER", "sample-0")


def own_project_id(registry: pathlib.Path | None = None) -> str | None:
    """The id of the project that repository belongs to, or None.

    Resolution order mirrors how the merge itself reasons: a project that
    CLAIMS the repository beats one that merely shares the folder name, and a
    contract or satellite project that happens to contain the name loses to
    both.
    """
    # `paths.REGISTRY`, never a path under the source tree — a sandboxed
    # registry has to be redirectable, and the resolver is used from inside
    # sandboxes.
    reg = registry or paths.REGISTRY
    try:
        projects = json.loads((reg / "projects.json").read_text(encoding="utf-8"))["projects"]
    except (OSError, ValueError, KeyError):
        return None
    by_repo = [p for p in projects
               if any(OWN_NWO in r for r in (p.get("repos") or []))
               or any(r.startswith(f"{OWN_NWO}:") for r in (p.get("membership_rules") or []))]
    if by_repo:
        return by_repo[0]["id"]
    by_folder = [p for p in projects if OWN_FOLDER in (p.get("folders") or [])]
    return by_folder[0]["id"] if by_folder else None


def assert_resolvable(check) -> str:
    """Resolve, or fail the suite with the reason rather than a KeyError."""
    pid = own_project_id()
    check("the suite's subject project resolves in the registry", pid is not None,
          f"no project claims {OWN_NWO} or the folder {OWN_FOLDER!r} — "
          f"the suites below measure this project, so they cannot run")
    return pid or ""
